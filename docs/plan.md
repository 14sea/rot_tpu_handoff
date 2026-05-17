# Dynamic FPGA reconfiguration on EP4CE6 — plan v2

## Context (revised 2026-05-15)

**Goal**: enable a running NEORV32 on AX301 to switch to a different
bitstream at runtime — either a **prebuilt** `.rbf` chosen from SD card,
or a `.rbf` it **generates on-chip** by editing LUT mask contents of a
base bitstream (NN inference use case: quantized weights → LUT TT
cells, no Quartus rebuild).

This is a single architecture serving two related capabilities:

- **Track A — SD as bitstream library.** Multiple prebuilt `.rbf` on SD
  (TPU, debug, alt-accelerator, etc.). On demand: select → copy to
  EPCS → ALTREMOTE_UPDATE swap.
- **Track B — On-chip LUT mask editor (Tier 2).** SD holds one base
  `.rbf` + an "edit list" of `(X, Y, N, mask)` tuples. CPU loads base
  to SDRAM, applies edits via on-chip C port of `LutCodec.predict_sram`
  + σ⁻¹ tables, recomputes CRC for affected frames, then runs the same
  EPCS-write + ALTREMOTE_UPDATE path.

Track A is a degenerate case of Track B (empty edit list). Both share
the EPCS + ALTREMOTE_UPDATE infrastructure (Phases 2-4); Track B adds
the LutCodec C port (Phase 7) + the `mode_g` dispatcher (Phase 8).

**Where bitstream RE actually contributes** (revised — was understated
in plan v1):

- **Track A**: ζ pipeline byte-identity gate makes baked-hash trust
  anchors deterministic across rebuilds. Useful but not load-bearing
  (Quartus + sha256sum would also work).
- **Track B**: **load-bearing** — Phase 7 ports `LutCodec` from
  `/home/test/EP4CE6/fuzz/bitstream.py` (predict_sram + σ⁻¹ + CRC) to
  C. Without the RE-mined per-position cell mappings + σ⁻¹ tables,
  the CPU cannot map "mask 0x4444 at X4Y4N0" to "flip these 16 (off,
  bp) cells in the .rbf". This is exactly what the RE work was for.

**Out of scope** (per user 2026-05-15):
- Tier 3 (full FASM compile on chip — porting all of fasm2rbf). Months
  of work, ~50 KB C + multi-MB tables.
- Tier 4 (on-chip Yosys/nextpnr). Not feasible on this hardware.
- Open-toolchain replacement of Quartus. See "Open-toolchain feasibility"
  section below — vendor IP unavoidable for ALTREMOTE_UPDATE.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  HOST bitstream (built by Quartus, ζ-verified)           │
│   ├── NEORV32 SoC + 32 MB SDRAM                          │
│   ├── wb_sha256              @0xF1000000  (existing)     │
│   ├── wb_altasmi_parallel    @0xF2000000  Phase 2 IP     │
│   ├── wb_altremote_update    @0xF3000000  Phase 2 IP     │
│   └── stage2 firmware in IMEM:                           │
│       ├── existing modes ('b', 'l', 's', 'd', 'B', …)    │
│       ├── mode 't': verify+load .rbf from SD → EPCS swap │  Track A
│       └── mode 'g': base + edit list → on-chip codec     │  Track B
│                     → SDRAM → CRC patch → EPCS → swap    │
└──────────────────────────────────────────────────────────┘
                         │
       ┌─────────────────┴─────────────────┐
       │ Track A: prebuilt                 │ Track B: edited
       ▼                                   ▼
SD: tpu.rbf | accel.rbf | debug.rbf   SD: base.rbf + edits.bin
     │                                     │  (NEW C port: LutCodec)
     │ sd_read_many()                      │ sd_read + apply_edits + CRC
     ▼                                     ▼
SDRAM scratch (368 KB)               SDRAM scratch (368 KB, edited in place)
     │ sha256_hw verify                    │ sha256_hw verify (optional)
     │ vs rot_golden_*_bitstream           │
     ▼                                     ▼
     └───────────────┬───────────────────┘
                     │
                     ▼
          wb_altasmi: erase + program EPCS region 1 (skip if hash match)
                     │
                     ▼
          wb_altremote_update: PAGE_SEL=1, RECONFIG_TRIGGER=1
                     │
                     ▼ (nCONFIG pulse, ~150 ms cold reset)
          TARGET bitstream live
```

## Tier scope (per user's clarification)

| Tier | Scope | Status in this plan |
|---|---|---|
| 1 | BIT-level patch (raw `(off, bp)` flip list + CRC) | Subsumed by Tier 2 |
| **2** | **LUT mask editor — `(X, Y, N, mask)` edit list, on-chip predict_sram + σ⁻¹** | **In scope (Phase 7+8)** |
| 3 | Full FASM compile (ROUTE / IOB / M9K / …) | Out of scope |
| 4 | Yosys/nextpnr on chip | Infeasible |

## Implementation phases (existing patterns reused)

### Phase 1 — ROT firmware: SD-side .rbf verify (Track A bring-up; mode 't', no EPCS)  ✅ DONE

First instance of Track A. Extends ROT to read a TPU `.rbf` from SD,
verify SHA-256 vs a baked golden, halt before any EPCS touch. The same
plumbing (sd_layout v3, sd_pack `--tpu-rbf`, gen_golden TPU section)
generalizes to any Track A `.rbf` — TPU is just the first concrete
target. The SD layout has slot capacity for 1 `.rbf`; future expansion
to N slots only needs more `(lba, sz)` fields in the header.

**Landed in patches/neorv32_rot/0001-Phase-1-….patch:**
- `/home/test/neorv32_rot/sw/stage2_loader/main.c`: `sd_boot_hdr` v3 + `mode_tpu_verify` handler — mirrors `mode_sd_boot` (lines 364–452). Reads from new SD region (LBA 8009, 720 sectors = 368,640 B) into SDRAM scratch at `0x41000000`, calls `sha256_hw()`, compares to `rot_golden_tpu_bitstream[8]`.
- `/home/test/neorv32_rot/sw/stage2_loader/rot_golden.h`: 4th 8-word constant (auto-generated, gitignored).
- `/home/test/neorv32_rot/host/sd_layout.py` v2→v3 (TPU_LBA=8009, TPU_MAX_SEC=720, 4th sha256 slot).
- `/home/test/neorv32_rot/host/sd_pack.py` (`--tpu-rbf` flag, 4th segment).
- `/home/test/neorv32_rot/host/gen_golden.py` (4th SECTIONS entry, env-overridable path, graceful zero-fallback).

**Validated**: build clean, 7832→9492 B IMEM (58% of 16 KB). Python sd_layout round-trip passes. End-to-end ex-Quartus test confirmed `5e87f3dd…` matches `sha256sum neorv32_tpu.rbf`.

**Effort consumed**: ~3 h.

### Phase 2 — ROT hardware: EPCS controller + ALTREMOTE_UPDATE  ✅ DONE

**Landed in patches/neorv32_rot/0005-Phase-2-….patch (2026-05-17):**

The Lite-Edition Quartus IP wizard turned out to need a fully-CLI
bootstrap path (no GUI step):

- `rtl/ip/epcs_ctrl.{v,qip}` — generated via `qmegawiz -silent` from
  a hand-crafted `// Retrieval info:` stub.  USE_ASMIBLOCK=ON drives
  the dedicated AS pins automatically.  ~80 KB clearbox-expanded
  implementation; saved into the repo so `quartus_sh --flow compile`
  is headless.
- `rtl/wb_altasmi_parallel.v` — thin XBUS wrapper at `0xF2000000`,
  register map `{CTRL, ADDR, DATAIN, STATUS, DATAOUT, ESTAT}`.
  Firmware (Phase 3 `epcs.c`) drives per-byte handshake; HDL is just
  ~80 LoC of bus glue.

The altremote_update IP couldn't be used in our first attempt because
the `qmegawiz -silent` flow we used **didn't pass the "Add support for
writing configuration parameters" option**, and without it the
generated wrapper drops `write_param`/`data_in` even for Cyclone IV E
REMOTE mode.  Phase 4.1 investigation (2026-05-17 PM, see
[[reference-rublock-complexity]]) located the ALSE 2023 App Note that
documents this checkbox in the wizard — meaning the IP IS usable from
Lite, we just need to re-run qmegawiz with the right options.  For
Phase 2 we shipped the workaround:

- `rtl/wb_altremote_update.v` — directly instantiates the
  `cycloneive_rublock` WYSIWYG primitive (7 ports), with a 29-bit
  shift FSM driving its serial interface.  Wrapper @ `0xF3000000`,
  register map `{DATA, CMD={SHIFT_IN_AND_RECONFIG, SHIFT_OUT_CAPTURE,
  RECONFIG_ONLY, RESET_TIMER}, STATUS}`.

**Caveat (uncovered in Phase 4.1)**: this direct-primitive wrapper only
exposes the 29-bit shift register, which writes the Update register
(next-reconfig settings).  The currently-running watchdog reads from
the Control register (loaded from `.pof` option bits at config time),
which is **read-only** at this layer.  Disabling the running watchdog
requires the megafunction's `param`/`data_in`/`write_param` protocol
(`param=3, data_in[0]=0, write_param`) — not available in this
wrapper.  See Phase 4 for the implications and [[reference-rublock-
complexity]] for the full failure log.

QSF additions: `USE_CONFIGURATION_DEVICE ON`,
`CYCLONEIII_CONFIGURATION_DEVICE EPCS16`,
`CYCLONEIII_CONFIGURATION_SCHEME "ACTIVE SERIAL"`,
`INTERNAL_FLASH_UPDATE_MODE REMOTE`.

**Fit results (slow 1200 mV 85 °C corner)**:
- LE: **8,819 / 10,320 (85 %)** ✅ under the jailbreak-die budget
  (+556 LE vs pre-Phase-2)
- Setup slack: −1.350 ns (was −0.927 pre-Phase-2; pre-existing
  NEORV32-core timing tightness made ~0.4 ns worse).  Closes at
  typical PVT; future timing-cleanup pass owed.
- 95 warnings (mostly clock-uncertainty info + a reserved-AS-pin
  conflict that Quartus correctly ignores)

**Effort consumed**: ~6 h (planned 6–8).  Dominated by reverse-
engineering the qmegawiz CLI flow (cf. [[feedback-quartus-clearbox-
megafunctions]]) and discovering that altremote_update REMOTE-mode
silently drops write-side ports in Lite Edition.

### Phase 3 — ROT firmware: EPCS write driver + post-write verify

**Adds:**
- `/home/test/neorv32_rot/sw/stage2_loader/epcs.c` + `epcs.h`:
  - `epcs_erase_sector(uint32_t addr)` — 64-KB erase via `0xF2000000` CMD reg, polls `WIP`.
  - `epcs_program_page(uint32_t addr, const uint8_t *buf)` — 256-byte page program.
  - `epcs_read(uint32_t addr, uint8_t *buf, uint32_t n)` — for verify.
  - `epcs_write_image(uint32_t base, const uint8_t *src, uint32_t len)` — erase + program loop with progress dots.

**Edits:**
- `main.c` `mode_t`: after SD-side hash OK, **first call `epcs_read` + `sha256_hw` on EPCS region 1 — if hash already matches `rot_golden_tpu_bitstream`, SKIP erase/program entirely** (idempotent boot, zero EPCS wear when nothing changed; protects EPCS's ~100k erase-cycle budget if this flow ever becomes auto-boot). Otherwise call `epcs_write_image(EPCS_TPU_BASE=0x80000, sdram_buf, 368011)`. Then `epcs_read` back into a second SDRAM buffer at `0x41100000`, run `sha256_hw()` again, compare to same `rot_golden_tpu_bitstream`. EPCS region 0 = ROT bitstream (untouched, address `0x000000`); region 1 = TPU at `0x80000` (configured as remote-update page 1).

**Pre-write SDRAM pattern test** (defense in depth):
- Existing `sdram_test()` at main.c:113 only exercises 64 words at `0x41F00000`; it does NOT cover TPU_SCRATCH_ADDR (0x41000000). Add a quick 32-word write/readback at TPU_SCRATCH_ADDR before the SD read in mode_t. Cheap (~µs) and rules out "stale SDRAM produces phantom hash mismatch" debugging.

**Test**: `[rot] EPCS write OK`, `[rot] EPCS-side sha256=… [OK]`. Pull SD mid-write → SHA mismatch on read-back → halt.

**Effort**: 6–8 h.

### Phase 4 — ROT firmware: trigger ALTREMOTE_UPDATE  🔴 DEFERRED (2026-05-17 PM)

**Phase 4.1 finding** (2026-05-17 afternoon, see [[reference-rublock-
complexity]]): the plan-v5 estimate "2–3 h, drive
`wb_altremote_update.CMD=SHIFT_IN_AND_RECONFIG`" turns out to be a
significant underestimate.  Empirical evidence on AX301 silicon:

1. After JTAG-volatile load of our bitstream (rublock instantiated),
   the FPGA **autonomously reconfigures back to EPCS page 0 within
   ~100-500 ms** of stage2 reaching its dispatcher prompt.  No `'r'`
   command needed.
2. Cause: the rublock hardware watchdog loads from `.pof` option bits
   at config time with `WD_EN=1` by default.  The current Control
   register (where the running WD lives) is read-only via our 29-bit
   shift wrapper; we can only write the Update register (next
   reconfig).  See [[reference-rublock-complexity]] for the four
   failed mitigation attempts (SHIFT_IN_ONLY with WD_EN=0, auto-pulser
   with and without `(* preserve *)`, direct counter→rsttimer wiring).
3. The proper API for runtime WD disable is the **megafunction**'s
   `param=3, data_in[0]=0, write_param` protocol — which our direct-
   primitive wrapper doesn't expose.

**Recovery path** (estimated 4–8 h vs original 2–3 h):
1. Re-run `qmegawiz` on `altremote_update` for Cyclone IV E REMOTE
   mode with **"Add support for writing configuration parameters"**
   explicitly enabled (it's a wizard checkbox; ALSE app note p. 3).
2. Rewrite `rtl/wb_altremote_update.v` as a Wishbone wrapper around the
   megafunction (not the primitive).  Expose registers for `param[2:0]`,
   `data_in[21:0]`, `read_source[1:0]`, plus trigger bits for
   `write_param`, `read_param`, `reconfig`, `reset_timer`.  Resource
   delta is small: megafunction adds ~83 LCs over the bare primitive,
   our current ~189 LC wrapper would be replaced with a thinner one
   around the megafunction.
3. `epcs.c` adds `epcs_remote_reconfig(page, ap_sel)` following the
   ALSE sequence: (a) write reg #4 = boot page byte address (caveat:
   may need to be the un-divided address per the Cyclone 10 LP note,
   verify on AX301), (b) write reg #3 with `data_in[0]=0` to inhibit
   WD, (c) raise `reconfig` for ≥250 ns.
4. Validate first on **EPCS cold-boot path** (`openFPGALoader -f`),
   not JTAG-volatile.  Hypothesis from Phase 4.1: rublock state after
   JTAG-volatile may differ from after EPCS cold-boot.  Cost: this
   overwrites factory ALINX, so first script a Quartus build of a
   small "Conf2" demo (e.g. blinky) to a second EPCS page so reconfig
   has a visible target without re-flashing.
5. `[rot] handing off to TPU bitstream…\r\n` + TX-empty drain + busy-
   loop pause (mirror the proven `mode_set_baud` pattern) is still
   the right firmware shape; that part of the original plan is fine.

**Watchdog policy** (avoid infinite-reconfig loops):
- ALTREMOTE_UPDATE supports `WATCHDOG_EN` + `WATCHDOG_TIMEOUT`. If the loaded image (TPU) doesn't write to the watchdog clear register within ~timeout seconds of CONF_DONE, FPGA self-falls-back to page 0 (ROT). If ROT then re-runs mode_t and re-triggers, infinite reboot.
- **For initial bring-up: set `WATCHDOG_EN=0`** (manual fallback only) by writing reg #3 first.  Removes the loop-trap class entirely.
- Future enablement (after TPU firmware grows a clear-routine): set `WATCHDOG_EN=1`, `WATCHDOG_TIMEOUT≥30s`, AND require TPU's stage2_tpu / Linux to pulse `reset_timer` (megafunction input) early in boot. Until then, leave disabled.

**Adds (original plan, deferred but conceptually still valid)**:
- `epcs.c`: `void epcs_remote_reconfig(uint32_t page)` — writes `PAGE_SEL=1` then `RECONFIG_TRIGGER=1` to `0xF3000000`. After this MMIO write, `nCONFIG` pulses low and the FPGA self-reconfigures from EPCS page 1; firmware never returns. UART prints `[rot] handing off to TPU bitstream…\r\n` and waits for TX-empty **THEN an extra busy-loop pause** before triggering — the TX-empty flag asserts when the shift register loads, not when the last STOP bit hits the wire (~87 µs at 115200-8N1). Mirror the proven pattern in `mode_set_baud` (main.c:326-328): `while (!(uart_ctrl[0] & TX_EMPTY)) { } ; for (volatile int i = 0; i < 2000; i++) { }` — the busy-loop covers worst-case PHY drain across baud rates.

**Watchdog policy** (avoid infinite-reconfig loops):
- ALTREMOTE_UPDATE supports `WATCHDOG_EN` + `WATCHDOG_TIMEOUT`. If the loaded image (TPU) doesn't write to the watchdog clear register within ~timeout seconds of CONF_DONE, FPGA self-falls-back to page 0 (ROT). If ROT then re-runs mode_t and re-triggers, infinite reboot.
- **For initial bring-up: set `WATCHDOG_EN=0`** (manual fallback only). Removes the loop-trap class entirely.
- Future enablement (after TPU firmware grows a clear-routine): set `WATCHDOG_EN=1`, `WATCHDOG_TIMEOUT≥30s`, AND require TPU's stage2_tpu / Linux to clear the watchdog (write to ALTREMOTE_UPDATE's `update` register early in boot). Until then, leave disabled.

**Future hardening — boot-attempt counter** (required before
WATCHDOG_EN=1 ships to non-bench users):

Watchdog enable on its own is not sufficient — if the loaded image
*always* fails to clear the watchdog (bad target firmware, dead
peripheral, runtime panic), the chip ping-pongs ROT ↔ target forever
even with WATCHDOG_EN=1, just at a slower cadence. The fix is a
persistent boot-attempt counter that ROT increments before reconfig
and the target zeroes after successful boot.

- **Storage**: AX301 has no I²C EEPROM or non-volatile scratchpad on
  the standard board, so the counter cannot live in DMEM (volatile)
  or on-chip flash (doesn't exist on EP4CE6E). Reserve a dedicated
  EPCS sector — e.g. `EPCS_BOOT_CTR_BASE=0x70000` (64 KB sector,
  one sector before TPU at 0x80000) — and store `(magic, counter)`
  as a packed u32. The wear concern is small (one sector erase +
  one program per reconfig × counter cap; ≪ EPCS 100k budget).
- **ROT flow** (added to mode_t / mode_g, before
  `epcs_remote_reconfig`):
  1. `epcs_read(EPCS_BOOT_CTR_BASE, …)` → `(magic, ctr)`.
  2. If `magic != BOOT_CTR_MAGIC` (uninitialized / corrupted),
     treat as `ctr=0`.
  3. If `ctr >= BOOT_CTR_MAX` (default 3): print
     `[rot] BOOT-ATTEMPT FUSE BLOWN (ctr=…) — entering console mode,
      refusing reconfig`. Stay in ROT, accept UART commands. Operator
     must explicitly `c` (clear counter) before next reconfig.
  4. Else: erase + write `(BOOT_CTR_MAGIC, ctr+1)`, then proceed to
     reconfig.
- **Target flow** (TPU stage2_tpu / Linux init): once peripherals
  are confirmed live (kernel reaches userspace, or stage2_tpu prints
  banner — pick the latest checkpoint that you trust as "I am OK"),
  call into ALTREMOTE_UPDATE's `update` register *and* erase+rewrite
  `(BOOT_CTR_MAGIC, 0)` to ROT's counter sector. Both operations are
  needed: the ALTREMOTE_UPDATE clear keeps the per-boot watchdog
  satisfied; the EPCS-counter clear releases the per-history fuse.

The threshold `BOOT_CTR_MAX=3` is a typical "transient flake vs
persistent failure" cutoff (3× ALTREMOTE_UPDATE fallbacks ≈ 0.5 s of
wasted time before the fuse blows; user gets a clear console prompt
rather than an infinite loop).

This pattern is deferred until after Phase 8 lands and the watchdog-
clear routine is wired into the target's boot path — premature
counter enablement on a hand-flashed bring-up board produces
console-lockouts the operator can't easily clear without JTAG.

**Test**: scope `nCONFIG` pin (or just observe UART silence then TPU stage2 banner ~150 ms later → kernel boot). Verify no extra dots/garbage at end of UART trace from truncated final byte.

**Effort**: **4–8 h** (revised 2026-05-17 PM after Phase 4.1) + 2 h
(boot-counter, deferred).  Original 2–3 h estimate assumed the direct-
primitive wrapper would suffice; Phase 4.1 proved otherwise.

### Phase 5 — Build automation: cross-repo orchestrator (`tpu-then-rot`)  ✅ DONE

Generalizes the build chain across three sibling repos. Currently
specialized to `tpu-then-rot` (TPU as the canonical Track A target);
will need a `*-then-rot` rename pattern when more Track A variants
land, and a separate `host-only` target once Track B (mode 'g') is
landed (host bitstream needs to be built once, then mode 'g' edits
arbitrary base `.rbf` at runtime).

**Landed in patches/neorv32_rot/0002-Phase-5-….patch:**
- `/home/test/neorv32_rot/Makefile` (top-level, new): `apply-patches` /
  `unapply-patches` / `tpu-bitstream` / `tpu-zeta-verify` / `tpu-copy`
  / `rot-firmware` / `rot-bitstream` / `rot-zeta-verify` / `tpu-then-rot`.
  Env-overridable: `TPU_REPO`, `EP4CE6_REPO`, `QUARTUS_BIN`, `XPACK_BIN`,
  `ZETA_VERIFY`.
- `gen_golden.py` extension: 4th SECTIONS entry, env-overridable path
  via `TPU_RBF_PATH`, graceful zero-fallback when `output/tpu.rbf` missing.

**Validated**: copying the existing `neorv32_tpu.rbf` to `output/tpu.rbf`
and rebuilding stage2 produces `rot_golden_tpu_bitstream` byte-matching
canonical `sha256sum neorv32_tpu.rbf`.

**Effort consumed**: ~2 h.

**Track B follow-up** (will land alongside Phase 7+8):
- New `host-bitstream` target — builds the host `.rbf` (Track A+B
  capable) without baking any specific target hash. The host's role is
  to be the always-resident bootstrap; targets vary at runtime.
- New `lutcodec-tables` target — emits the C arrays for σ⁻¹ + per-
  position predict_sram from `/home/test/EP4CE6/results/sigma_inv_fb8_groups.json`
  + `LutCodec.from_cram_model` Python output. Generated `lutcodec_data.c`
  + `.h` get committed (or auto-regenerated at build time).

### Phase 6 — HW validation flow

**SD prep**:
```
python3 /home/test/neorv32_rot/host/sd_pack.py \
    --image Image --dtb …dtb --initrd …cpio.gz \
    --tpu-rbf neorv32_tpu.rbf --device /dev/sdX
```

**JTAG flash sequence (one-time)**:
```
openFPGALoader -c usb-blaster -f neorv32_rot/output/neorv32_demo.rbf
```
Programs ROT into EPCS region 0. EPCS region 1 left blank; ROT itself populates it from SD on first boot.

**Expected UART trace**:
```
[stage2] ready - RV32IMAC NEORV32 loader
[stage2] CRTM locked — waiting for host command
> t
[rot] TPU sha256=… [OK]
[rot] EPCS erase 0x80000..0xD8000  ............
[rot] EPCS write 368011 B  ............
[rot] EPCS-side sha256=… [OK]
[rot] handing off to TPU bitstream
<nCONFIG pulse, ~150 ms silence>
[stage2-tpu] ready ...
<Linux boot, /dev/npu present>
```

**Recovery / fallback**: ALTREMOTE_UPDATE supports `WATCHDOG_EN` + `WATCHDOG_TIMEOUT`. If TPU bitstream fails CRC at config time, or watchdog fires before the loaded image clears it, EPCS hardware auto-falls back to page 0 (ROT) — by-design Cyclone IV behavior. ROT detects fallback by reading `STATUS` reg of altremote_update (`asmi_busy`/`current_state`) and prints `[rot] FALLBACK — TPU image bad, staying in ROT`.

**Effort**: 3–4 h.

### Phase 7 — Track B: LutCodec C port + σ⁻¹ + CRC  ✅ DONE

The bitstream-RE-load-bearing piece. Port the minimum subset of
`/home/test/EP4CE6/fuzz/bitstream.py` needed for "given (X, Y, N,
16-bit mask) compute the (off, bp) cells to set/clear" + recompute
frame CRCs.

**Landed in patches/neorv32_rot/0003-Phase-7-….patch (2026-05-16):**
- `host/gen_lutcodec_data.py` — emits `sw/stage2_loader/lutcodec_data.{c,h}`
  (auto-gen, gitignored) with a packed 1152-byte σ⁻¹ table + COLUMN_BASE
  / LAB_Y / SLOT_BASE geometry, sourced from `$EP4CE6_REPO`.
- `sw/stage2_loader/lutcodec.{c,h}` — compact LE descriptor (12 B); cells
  computed on-demand from σ⁻¹ + formulas (no per-position cell array).
  Exposes `lc_init` / `lut_apply_mask` / `lut_apply_with_canon`.
- `sw/stage2_loader/crc16.{c,h}` — `crc16_rbf_frame` + `lc_patch_rbf_crc{,_frames}`
  + dirty-bitmap accumulator (`lc_mark_dirty`).
- `host/test_lutcodec_c.py` — ctypes cross-test, three regimes:
  RANDOM (100 tuples), EDGE (26 corner cases — mask∈{0,0xFFFF}, slot-1
  wrap boundaries at y=3/6/9/12/18/21), COMPOSE (8 chained edits +
  dirty-frame CRC). 127/127 byte-identical vs Python reference.
- `rtl/ax301_top.vhd` + `sw/stage2_loader/Makefile` — IMEM 16K→32K bump.

**Effort consumed**: ~4 h (planned 8–12).  Smaller than budgeted
because σ⁻¹ table compressed to 1.2 KB (not 30 KB) once restricted to
pin-friendly columns, and on-chip cell computation from formulas
(rather than precomputed per-position) further shrunk .rodata.

`main.c` left untouched per Phase 8 split — `lutcodec/crc16` symbols
dead-stripped from current stage2 ELF (still 9492 B) until mode 'g'
references them.

**Anti-pattern to reject** — "move codec tables to SD card to save
M9K / IMEM":

A future reader (human or LLM) will eventually suggest that the
~1.2 KB σ⁻¹ + geometry tables don't need to live in IMEM — they
could be loaded from SD into SDRAM at mode 'g' entry and the IMEM
bump rolled back. **Don't.** This proposal looks like a sound
memory-budget optimization but violates the project's foundational
trust model:

- The whole CRTM (Chain-of-Trust Root Measurement) discipline of
  this codebase — see CLAUDE.md, §Phase 1, §Phase 5, `rot_golden.h`
  — is that **trusted measurement code and its constants are baked
  into the bitstream**. Tampering requires JTAG-side EPCS rewrite,
  which is a physically-observable event.
- σ⁻¹ + geometry tables are part of the codec semantics: they
  determine which CRAM cells `lut_apply_with_canon` flips. An
  attacker who can swap the SD card can hand the running ROT a
  table that maps "(x=10, y=2, n=0, mask=0xAAAA)" to entirely
  different CRAM cells than Quartus did. The base bitstream's
  SHA-256 still matches, the post-edit hash is printed to UART
  (not compared), the EPCS image is byte-coherent (CRC is repaired
  downstream of the lookup) — and the freshly-configured FPGA does
  whatever the attacker's table dictates.
- This is not a hypothetical concern. mode 'g' is the *only* path
  in this design that takes design-time-unknown bitstream as input
  and writes it to EPCS without a baked-golden hash check. The
  codec tables are the last anchor that ties the EPCS output back
  to the design-time CRAM model. Moving them to SD removes that
  anchor.
- The empirical premise of the "save M9K" suggestion is also wrong:
  the actual footprint is 1.2 KB (not the plan's original 30 KB
  budget). The IMEM bump from 16K to 32K still lands — but slack
  is huge, so it isn't a cost worth re-engineering CRTM around.

If genuinely-new bitstream-RE evidence accumulates and the codec
tables need to evolve in production without re-synth, the
*correct* mechanism is: bake a public key into stage2's .rodata,
ship signed table updates on SD, verify the signature on load.
This preserves the trust anchor at the cost of one Ed25519-verify.
That work is out of scope here and would land alongside Phase 8's
"edit list authentication" future-hardening row (which has the
same structure).

**Pre-landing spec (preserved for archaeological reference)** —
the rest of this section was the design contract before Phase 7
shipped. Superseded by the "Landed in patches/neorv32_rot/0003" and
"Anti-pattern to reject" sections above; kept verbatim because some
items (memory budget, validation gate) are contradicted by the
actual implementation and the contrast is informative.

<details><summary>Original Phase 7 spec (pre-implementation)</summary>

- `sw/stage2_loader/lutcodec.c` + `lutcodec.h`:
  - `lut_codec_t lc_init(uint16_t x, uint16_t y, uint16_t n)` —
    computes the per-LE 16 minterm cell offsets via the same column-
    anchor + Y/N stride math as `LutCodec.from_cram_model`. Output:
    16 (off, bp) tuples cached in the struct. *(Shipped variant: on-
    chip formula instead of precomputed per-LE arrays — 1.2 KB
    σ⁻¹ vs the originally planned ~30 KB per-position tables.)*
  - `void lut_apply_mask(uint8_t *rbf, const lut_codec_t *lc, uint16_t mask)`
    — writes 16 bits absolutely (not XOR-delta) per the mask.
    *(Shipped variant: XOR-from-zero-baseline semantics, equivalent
    to Python LutCodec.write_tt; "absolute" framing was redundant.)*
  - `int lut_apply_with_canon(uint8_t *rbf, uint16_t x, uint16_t y,
    uint16_t n, uint16_t mask)` — wraps the above + applies σ⁻¹
    canonicalization for asymmetric masks at left-edge columns
    (X∈{3,4,6,7}). For pin-friendly columns (X∈{10,16,22,28}), σ⁻¹
    is a no-op. *(See σ⁻¹ correction in §Open-toolchain footnote —
    σ⁻¹ as defined by `_sigma_inv_lookup` is in fact required at
    pin-friendly cols; it's the canon-2input layer that's the no-op.)*
- `sw/stage2_loader/lutcodec_data.c` + `.h` — auto-generated from
  `/home/test/EP4CE6/results/sigma_inv_fb8_groups.json` (~2 KB) plus
  any per-position canonicalization tables needed (per `Pitfall #16`).
  Build-time generator: `host/gen_lutcodec_data.py` calls into the
  Python LutCodec to emit C arrays.
- `sw/stage2_loader/crc16.c` + `.h` — CRC-16/IBM, poly 0x8005
  (reflected 0xA001), init 0xFE54, frames 25..1751 (208 data bytes
  + 2 CRC). Direct port of `patch_rbf_crc()` from `bitstream.py`.
  ~50 lines C. Track which frames were modified during edit-apply,
  recompute CRC only for those.

**Validation gate** (pre-landing): round-trip test against Python
LutCodec.  `host/test_lutcodec_c.py`:
1. Generate 100 random `(x, y, n, mask)` tuples for valid CE6 LE
   positions.
2. For each, compute the modified `.rbf` via Python `LutCodec.write_tt`.
3. Compute the same modification via the C codec (cross-compile
   `lutcodec.c` for host x86_64, link as Python ctypes module).
4. Assert byte-identical RBF output.

*(Shipped variant: three regimes — RANDOM (100) + EDGE (26 corner
cases) + COMPOSE (8 chained edits + dirty-frame CRC) = 127/127
byte-identical. The COMPOSE regime caught a real semantic
discrepancy in the dirty-frame CRC variant during test development
— see commit message of 0003-Phase-7.)*

**Memory budget on NEORV32** (pre-landing estimate):
- IMEM 16 KB: stage2 currently 9.5 KB; +lutcodec ~3 KB code → ~12.5 KB.
  Tight but fits.
- σ⁻¹ + per-position tables: ~30 KB. Goes in `.rodata` of stage2,
  which lives in IMEM_INIT_FILE → bitstream. **OVERRUNS 16 KB IMEM**.
  Solution: bump IMEM_INIT_FILE to 32 KB (NEORV32 generic
  `MEM_INT_IMEM_SIZE`). Adds ~3.5k LE per NEORV32 docs (M9K-backed).
  Still fits on jailbreak die (10,320 LE total).
- SDRAM scratch: 2× 368 KB buffers (one for source, one for output of
  apply_lut). Existing 32 MB SDRAM more than enough.

*(Shipped variant: ~1.2 KB σ⁻¹ + ~80 B geometry + ~3 KB code ≈
4.3 KB Phase 7 footprint, not 30+ KB. IMEM bump still landed for
margin and to unblock Phase 8 wiring, but cost is 1 extra M9K
block, not multi-k-LE.)*

**Effort** (pre-landing): 4–6 h (port) + 4–6 h (validation + IMEM
bump) = ~8–12 h.  *(Actual: ~4 h; smaller table footprint shrank
the validation + tuning loop.)*

</details>

### Phase 8 — Track B: `mode_g` (generate) handler  ✅ DONE (software path)

Wire Phase 7 into stage2's dispatcher.

**Landed in patches/neorv32_rot/0004-Phase-8-….patch (2026-05-16):**
- `sw/stage2_loader/main.c` — `mode_generate` implements steps 1, 2, 2a,
  3, 5, 6, 7 from the spec below; halts before step 8 with the same gate
  Phase 1's `mode_tpu_verify` uses (Phases 2-4 EPCS/ALTREMOTE pending).
  `struct sd_boot_hdr` bumped to layout v4 (`reserved[2]` → `edits_lba`
  + `edits_sz`).  'g' wired into the main dispatcher.
- `sw/stage2_loader/crc32_iso.{c,h}` — extracted from main.c so the host
  test can ctypes-link the exact CRC code stage2 runs.  Exposes
  `crc32_update` / `crc32` / `crc32_two`.  The xmodem upload path reuses
  the same routine (it was already CRC-32/ISO-HDLC).
- `host/sd_layout.py` — `LAYOUT_VERSION=4` adds `EDITS_LBA=8729`,
  `EDITS_MAX_SEC=64` (32 KB cap).  `build_header` / `parse_header` /
  `verify_layout` carry `edits_lba` + `edits_sz` in the previously-
  reserved [56..64) header bytes; no hash-slot reflow.
- `host/sd_pack.py` — `--edits PATH` flag (auto-discovers
  `output/edits.bin` if present); slot-fit check; segment write.
- `host/gen_edits.py` — emits `edits.bin` from JSON manifest or CLI
  tuples; whitelist-validates against the same PIN_X × LAB_Y_CE6 ×
  even-N set on-chip `lc_init` enforces.
- `host/test_edits_bin.py` — round-trip + cross-CRC gate.  Compiles
  `crc32_iso.c` for host x86_64 via gcc, ctypes-loads it, asserts
  that on-chip `crc32` / `crc32_two` matches Python `binascii.crc32`
  byte-for-byte (including the standard `'123456789'` → `0xCBF43926`
  vector).  5/5 negative cases (bad magic / bad version / CRC flip /
  truncation / under-header) and 5/5 whitelist rejections detected.

**Validated**: build clean, 9492→13908 B IMEM (43% of 32 KB); 127/127
LutCodec byte-identity still green; round-trip via `make
unapply-patches` + `make apply-patches` reproduces the same head.

**Effort consumed**: ~3.5 h.  Smaller than the 4–6 h budget because the
existing xmodem CRC-32 routine was already CRC-32/ISO-HDLC (just had
to factor into a separate TU for test reach), and Phase 7 had already
solved the dirty-bitmap + per-frame CRC pieces.

**Future-hardening rows** (deferred per CRTM principle — same
mechanism as Phase 7's "Anti-pattern to reject" footnote):

1. **`editable_les.bin` allowlist on SD**.  Plan §Phase 8 mentioned
   a per-base sidecar that lists which LE positions the base
   bitstream marked editable; mode_g would cross-check each
   `(x,y,n)` against it before applying.  Initial bring-up relies on
   `lc_init`'s static pin-friendly whitelist (`LC_ERR_X/Y/N`).  This
   is fine when the base bitstream is built with Quartus pinning
   *every* NN-weight LUT to a pin-friendly column — which is the
   Phase 7 design assumption.  An allowlist lands once base
   bitstreams begin including non-NN-weight LUTs at pin-friendly
   positions that mode_g must NOT touch.
2. **Ed25519 over edits.bin**.  CRC-32 is integrity, not
   authentication.  An attacker who can rewrite edits.bin can also
   rewrite the CRC.  Authentication remains upstream (signed by the
   user's NN compiler).  On-chip verify with a public key baked
   into stage2's .rodata would land alongside the analogous
   trust-anchor row for codec table updates noted in Phase 7.

**Pre-landing spec (preserved for archaeological reference)** — the
rest of this section was the design contract before Phase 8 shipped.
Superseded by the "Landed in patches/neorv32_rot/0004" block above;
kept verbatim because the trust-boundary discussion is what
motivates the CRC-32 gate.

<details><summary>Original Phase 8 spec (pre-implementation)</summary>

**New mode 'g' in main.c**:
```
mode_generate:
  1. Read base.rbf from SD region 0 → SDRAM[BASE_SCRATCH=0x41000000]   (~0.7s @ ~0.5 MB/s SD)
  2. Read edits.bin from SD (small, 4-32 KB)                           (negligible)
  2a. Verify edits.bin: magic + version + CRC-32 (see integrity gate)  (~30 µs)
  3. SHA256 verify base.rbf vs rot_golden_base[8]                      (~50 ms)
  4. Pre-erase EPCS region 1 hash check (skip if already up-to-date)
  5. For each edit (x, y, n, mask) tuple:
       lut_apply_with_canon(BASE_SCRATCH, x, y, n, mask)
       mark frame as dirty
  6. patch_rbf_crc(BASE_SCRATCH) — only dirty frames                   (~10 ms)
  7. SHA256 of result → print to UART (informational only; no golden
     check, since the result is by definition design-time-unknown)
  8. epcs_write_image (Phase 3 reuse), with progress dots               (~30s flash)
  9. epcs_read + sha256 verify                                          (~5s)
  10. epcs_remote_reconfig (Phase 4 reuse)                              (~150 ms cold reset)
```

**Edit list format** (`edits.bin` on SD): 16-byte header + packed
8-byte records:
```c
struct edits_hdr {
  uint32_t magic;      // 0x45444954 ('EDIT' big-endian)
  uint32_t version;    // 1
  uint32_t n_records;  // count of edit_rec following the header
  uint32_t crc32;      // CRC-32/ISO-HDLC over [version | n_records | records]
                       // (the magic itself is excluded so a partial-read of
                       //  the first 4 bytes can't pass a CRC check by luck)
};
struct edit_rec {
  uint16_t x;          // LAB X (3..31, must be in CE6 whitelist)
  uint16_t y;          // LAB Y (2..21, must be in CE6 whitelist)
  uint16_t n;          // LE N (0,2,4,…,30; even per Quartus convention)
  uint16_t mask;       // 16-bit truth table
};
```

`mode_g` rejects records whose (x,y,n) lie outside the CE6 whitelist
or aren't pre-marked editable. The "editable LE region" is determined
at base-bitstream-build time (a QSF set_location_assignment manifest
emitted as a sidecar `editable_les.bin` on SD; mode_g cross-checks each
edit against this allowlist).

**Integrity (not authentication) gate on edits.bin**:

Before any edit is applied, `mode_g`:
1. Checks `magic == 0x45444954` — rejects unrelated files.
2. Validates `version == 1` — guards format evolution.
3. Recomputes CRC-32/ISO-HDLC over `version | n_records | records` and
   asserts equality with `crc32`. Mismatch → halt with
   `[!] edits.bin CRC FAIL (computed=… expected=…) — refusing to apply`.

Failure mode this closes: an SD read-side bit flip in one mask field
would otherwise produce a corrupted `(x, y, n, mask)` tuple that
`lut_apply_with_canon` faithfully applies → `patch_rbf_crc` recomputes
correct frame CRCs over the corrupted data → EPCS is programmed with a
**well-formed-but-wrong** bitstream. Frame CRC repair *legitimizes*
the corruption because it operates downstream of the edit application.
A 4-byte CRC32 header over edits.bin is the smallest mechanism that
detects this; ~30 µs to verify on NEORV32, ~15 lines of C reusing the
existing CRC pattern (different polynomial — CRC-32/ISO-HDLC, not the
CRC-16 RBF-frame poly).

**Trust boundary**:
- Base bitstream: SHA-256-verified vs baked golden (high trust).
- Edit list integrity: CRC-32 detects accidental corruption (SD read
  flips, truncation, wrong file). **CRC-32 is NOT authentication** —
  an attacker who can rewrite edits.bin can also rewrite the CRC.
  Authentication remains upstream of ROT (signed by user's NN
  compiler). Future hardening: add Ed25519 signature over edits.bin
  with the public key baked into stage2's .rodata alongside
  rot_golden_*; out of scope for initial bring-up.
- Output bitstream: hash printed to UART for forensics; ALTREMOTE_UPDATE
  watchdog (if enabled) catches malformed output that fails to
  configure.

**Test sequence**:
1. Take a base `.rbf` whose Quartus build pinned 16 NN-weight LUTs at
   known positions (e.g., LUT positions in a small region recorded by
   the synth tool).
2. Generate `edits.bin` writing 16 distinct masks.
3. Boot ROT (host bitstream), send `g` over UART.
4. Observe: `[rot] base sha256=… [OK]`, `[rot] applying 16 edits…`,
   `[rot] result sha256=…`, EPCS write progress, reconfig pulse.
5. After cold reset: read out one of the LUT-backed registers via the
   target bitstream's debug port. Verify the LUT masks took effect
   (output of LUT(input) matches the new mask's truth table).

**Effort**: 4–6 h (mode handler + SD reading + integration test).

</details>

### Phase 9 — EPCS slot rotation (deferred; high-frequency mode 'g')

**Problem**: EPCS sectors are rated for ~100,000 erase cycles.
Phase 8 lands with a single target slot at `EPCS_TPU_BASE=0x80000`,
which is fine for occasional reflash (per-day, per-hour). But the
Track B use case "NN-weight LUT mask edits via mode 'g'" can want
much higher cadence — once-per-minute adjustment from a feedback
loop wears one sector to its limit in ≈70 days
(60 × 24 × 70 ≈ 100,800). Defending against this requires spreading
the writes across multiple sectors.

**Mechanism**: Cyclone IV E ALTREMOTE_UPDATE supports per-image
PAGE_SEL programming; the page-N reconfig pin assignment determines
which EPCS offset the FPGA loads on `RECONFIG_TRIGGER`. Carve EPCS16
(2 MB) into slots:

```
0x00000  page 0  ROT bitstream (immutable; programmed via JTAG)
0x80000  page 1  TARGET slot A — application
0xF0000  page 2  TARGET slot B
0x160000 page 3  TARGET slot C
0x1D0000 page 4  TARGET slot D
```

(Each ~360 KB target image + ~24 KB margin = 384 KB stride. Four
slots after ROT fit in 2 MB.) Add a small u32 counter in a
dedicated EPCS sector (reused from the Phase 4 boot-attempt counter
sector if landed; otherwise a new one): `(magic, next_slot)`.

**mode_g flow change** (vs Phase 8):
1. Read `(magic, next_slot)` from `EPCS_SLOT_CTR_BASE`. Treat
   missing magic as `next_slot = 1`.
2. After SHA-verifying the generated bitstream, write it to slot
   `next_slot` (not always 1).
3. Update `(magic, (next_slot % 4) + 1)` so the next reconfig
   targets a different slot.
4. Call `epcs_remote_reconfig(next_slot)` with the freshly-written
   page number.

**Wear amortization**: 4 slots → ≈4× lifetime → ≈280 days at
once-per-minute, or ≈75 years at once-per-hour. Combined with
the Phase 3 pre-write hash check (which skips erase when the
target slot already has the right image), the realized wear is
usually much lower than the worst case.

**Trade-offs**:
- Quartus ALTREMOTE_UPDATE megawizard must be reconfigured for
  `Number of pages = 5` (page 0 ROT + 4 target slots). Page count
  is set at synthesis time on Cyclone IV E; runtime PAGE_SEL just
  picks which configured page to jump to.
- ROT firmware grows by ~100 lines (slot-counter read/write +
  page selection). Negligible IMEM impact.
- Trust-anchor implication: the `rot_golden_*` hash must still match
  whatever was last written to *any* slot. If different applications
  live in different slots (TPU in A, accelerator in B, …), each one
  needs its own `rot_golden_*[8]` constant. Phase 5's gen_golden.py
  already accepts multiple sections — extending to N target slots
  is just more SECTIONS entries.

**Triggering condition** (so this is not done speculatively):
land Phase 9 when bench data shows mode 'g' is being invoked at
**> 10× per day** on a deployed unit. Earlier than that, single-slot
EPCS wear is irrelevant.

**Effort**: 4–6 h (Quartus IP reconfig + slot-counter logic + test).
Deferred; not part of Track B initial bring-up.

## Failure-mode handling matrix

| Failure | Detection | Action / UART |
|---|---|---|
| SD read error | `sd_read_many()` non-zero | `[!] SD read FAIL lba=…` halt |
| SDRAM scratch corrupted | pre-read pattern test (Phase 3 add) | `[!] SDRAM scratch test FAIL` halt |
| SD-side base SHA mismatch (Track A or B) | golden compare in mode_t / mode_g | `[!] base SHA mismatch — refusing EPCS write` halt |
| edits.bin corrupted (Track B) | CRC-32 header check before record dispatch | `[!] edits.bin CRC FAIL (computed=… expected=…) — refusing to apply` halt. Closes the "CRC repair legitimizes corrupted edit" hole (see Phase 8 integrity gate). |
| Edit position outside whitelist (Track B) | `editable_les.bin` allowlist check | `[!] edit (X,Y,N) not in editable region — halt` |
| Edit at unmapped CE6 position (Track B) | `lc_init` returns NULL | `[!] (X,Y,N) outside CE6 LE whitelist — halt` |
| Asymmetric mask at unmined σ⁻¹ position (Track B) | canon table KeyError-equivalent in C | `[!] σ⁻¹ not mined for (X,Y,N) — repin LUT to clean column` halt |
| EPCS already up-to-date | pre-erase region-1 hash match | `[rot] EPCS region 1 already matches — skipping write`, jump straight to reconfig |
| EPCS erase/program timeout | `WIP` poll exceeds N ms | `[!] EPCS WIP stuck — halt` |
| EPCS post-write SHA mismatch | second hash compare | `[!] EPCS verify failed — re-erasing region 1 to 0xFF` halt |
| ALTREMOTE_UPDATE error | altremote `current_state` ≠ idle after trigger | `[!] reconfig refused` halt in ROT |
| Target boots but hangs | watchdog in altremote_update (WATCHDOG_EN=1) | HW auto-fallback to page 0; ROT prints `[rot] FALLBACK` (initial bring-up keeps WATCHDOG_EN=0 to avoid loop-trap) |
| Persistent target failure (3+ fallbacks) | EPCS boot-attempt counter ≥ BOOT_CTR_MAX | `[rot] BOOT-ATTEMPT FUSE BLOWN — entering console mode, refusing reconfig`. Operator clears via UART command. (Deferred — lands with WATCHDOG_EN=1, not initial bring-up.) |
| Truncated final UART byte | terminal sees garbage | TX-empty wait + busy-loop drain pause before nCONFIG (Phase 4) |
| LutCodec C-vs-Python divergence | `host/test_lutcodec_c.py` cross-test | CI-time gate; fail commit if any of 100 random tuples diverge |

## LE-budget escape hatches (if Phase 2 fit fails)

If new IP pushes ROT past 10,320 LE on jailbreak die (or 6,272 on standard
EP4CE6 if jailbreak-route mining is incomplete), trim in this order:
1. Drop `mode_uboot`, `mode_set_baud`, `sd_dump`, `sd_smoke` → IMEM-only,
   no LE saved but frees code space for EPCS driver.
2. Set `IO_GPIO_NUM = 0` in NEORV32 generics (saves ~50 LE).
3. Drop NEORV32 `IO_TWI_EN`, `IO_PWM_EN`, etc. — keep only UART + SPI (SD)
   + new EPCS SPI + Wishbone XBUS.
4. Reduce SHA-256 from iterative-66-cyc to streaming version (saves ~1k LE
   at cost of throughput; throughput not critical for one-shot 360-KB
   verify).
5. Last resort: drop SHA hardware entirely, use NEORV32 software SHA
   (saves ~3.5k LE, costs ~500 ms per 360-KB hash — still acceptable for
   one-shot boot).

## Critical files

- `/home/test/neorv32_rot/sw/stage2_loader/main.c` — Phase 1 + 3 + 4 firmware
- `/home/test/neorv32_rot/sw/stage2_loader/rot_golden.h` — Phase 1 hash constant
- `/home/test/neorv32_rot/sw/stage2_loader/sha256_hw.c/h` — REUSE for verify
- `/home/test/neorv32_rot/sw/stage2_loader/sd.c/h` — REUSE for SD read
- `/home/test/neorv32_rot/sw/stage2_loader/epcs.c/h` — Phase 3+4 NEW
- `/home/test/neorv32_rot/rtl/ax301_top.vhd` — Phase 2 IP wiring
- `/home/test/neorv32_rot/quartus/neorv32_demo.qsf` — Phase 2 config-mode
- `/home/test/neorv32_rot/host/sd_layout.py` + `sd_pack.py` — Phase 1 layout
- `/home/test/neorv32_rot/host/gen_golden.py` — Phase 5 build automation
- `/home/test/EP4CE6/scripts/bit_workaround/zeta_pipeline.py` — REUSE deterministic build (NOT modified)

## Verification plan

End-to-end test on AX301 hardware:

1. Run `make tpu-then-rot` — produces a deterministic ROT bitstream whose
   embedded golden hash matches the freshly-built TPU bitstream.
2. `python3 host/sd_pack.py … --tpu-rbf neorv32_tpu.rbf` — packs SD card
   with 4 payloads (Image, DTB, initrd, TPU.rbf).
3. `openFPGALoader -c usb-blaster -f neorv32_rot/output/neorv32_demo.rbf`
   — flashes ROT to EPCS page 0.
4. Power-cycle AX301; observe ROT banner over UART.
5. Send `t` over UART; observe expected trace from Phase 6.
6. After TPU comes up, run `mnist` or `cnn` shell command on TPU's Linux
   to confirm `/dev/npu` is functional — closes the loop.
7. Tamper test: corrupt one byte in `tpu.rbf` on SD; re-insert; send `t`;
   expect `[!] TPU SHA mismatch` halt (no EPCS write attempted).
8. Bad-bitstream test: write garbage to EPCS region 1 manually via JTAG;
   send `t` (will succeed and overwrite); separately, write garbage AND
   skip the SD verify path; observe ALTREMOTE_UPDATE watchdog fallback to
   page 0 + ROT's `[rot] FALLBACK` print.

## Open-toolchain (Yosys + nextpnr + np2fasm + fasm2rbf) feasibility

**Short answer: NO, this project cannot escape Quartus today.** Three
independent blockers, each sufficient on its own:

1. **Routing density on NEORV32-class designs.** Per
   `/home/test/EP4CE6/CLAUDE.md` Phase 7 (`Native: blocked on chipdb
   routing-model density at NEORV32 scale`) and memo
   `post_stage0_neorv32_linux_plan`: nextpnr-generic with the existing
   chipdb cannot route a 4712-LE / 19-M9K NEORV32 design. Both ROT (~5.5k
   LE post-Phase-2) and TPU (5,431 LE) sit firmly in NEORV32-class
   territory. The open toolchain works for small/medium fixtures
   (registered AND, 23-bit carry counter, 2-LAB cross-LAB designs) but
   not for full SoC builds.

2. **ALTREMOTE_UPDATE is opaque vendor IP.** `altremote_update` and
   `altasmi_parallel` are Altera megafunctions whose CRAM footprint is
   NOT in `results/route_cells_full.json` or any mining sidecar. The
   open toolchain cannot synthesize them. RE'ing the configuration
   controller is unmined territory and would be months of additional
   work — and even then, you'd still need vendor-equivalent IP to drive
   the ASMI pin path.

3. **Dedicated config-controller pins (DCLK / DATA0 / nCONFIG / nCSO /
   ASDO) are not fabric-reachable.** Cyclone IV E's configuration
   subsystem is a separate hard block; user logic cannot drive its pins.
   Workarounds (passive serial bypass via GPIO loop-back) are not
   electrically supported on this die. Without these pins, no self-
   reconfiguration mechanism exists at all — open or closed.

**What the bitstream RE work DOES contribute** (load-bearing in plan v2):
- **Phase 7 (Track B)**: the LutCodec C port directly reuses
  `LutCodec.from_cram_model` + `predict_sram` math + `sigma_inv_fb8_groups.json`
  table from `/home/test/EP4CE6/`. **Without the RE-mined per-position
  cell mapping + σ⁻¹ tables, mode 'g' cannot exist** — the CPU has no
  way to convert "(x, y, n, mask)" into "flip these CRAM cells."
  This is the single application in this project where bitstream RE is
  not just nice-to-have but architecturally required.
- **Phase 5 (Track A)**: ζ pipeline byte-identity gate makes baked-hash
  trust anchors stable across rebuilds. Useful but not load-bearing
  (Quartus + sha256sum would also work).
- **General**: ζ regression corpus protects the EP4CE6 RE work this
  project depends on from drift.

**What's NOT relevant** to this project:
- The `--canon-2input-unique-aware` etc. work-in-progress (P5b/P5c)
  targets cross-LAB σ⁻¹ closure for small designs at left-edge
  columns. For Phase 7's NN-weight LUT use case, the recommended
  practice is to **pin LUTs to clean columns** (X∈{10,16,22,28}) at
  Quartus time so the **canon-2input asymmetric-mask layer** is a
  no-op — sidesteps the entire Pitfall #16 axis.

  **Correction (Phase 7 implementation, 2026-05-16)**: the σ⁻¹
  permutation in `_sigma_inv_lookup(foff, fb8, group)` is NOT a no-op
  at pin-friendly columns — only ~8% (96/1152) of pin-friendly
  (x, y, n) triples have identity σ⁻¹.  The pin-friendly choice
  sidesteps the canon-2input layer (a 35-cell transition for
  asymmetric Quartus emission), which IS a no-op at these columns —
  but the per-position σ⁻¹ permutation has to be carried in the C
  port's tables either way.  Final on-chip footprint: 1152 bytes
  (packed σ⁻¹) + ~80 bytes of geometry tables, well under the 30 KB
  plan estimate.  The IMEM 16K→32K bump still lands (per the Phase
  7 contract above) but slack is now huge — Phase 8 has room for the
  mode 'g' handler without further generic bumps.

**If pure-open-toolchain self-reconfig were actually required**
(years-out research path):
- Mine the configuration controller's CRAM footprint (currently 0%
  coverage). Memo `jtag_phase2c_sld_hub_no_silicon_backdoor` documents
  why JTAG-side backdoor was falsified; you'd need a different attack.
- Reimplement EPCS read/write protocol in custom RTL using GPIO pins
  routed to the EPCS chip (which on AX301 might be physically wired
  only to dedicated config pins — needs board-level confirmation).
- Replace ALTREMOTE_UPDATE's reset-to-page-N protocol with a custom
  reset controller — but the per-die config-controller acceptance
  protocol is a hard-IP secret, not bitstream-CRAM-driven.

This is well beyond the scope of "feasibility analysis for an existing
ROT/TPU pair." Quartus stays.

## Effort estimate (revised)

| Phase | Effort | Status |
|---|---|---|
| 1: SD-side .rbf verify (Track A) | 3–4 h | ✅ done (~3 h) |
| 2: Quartus IP (altasmi via qmegawiz CLI + rublock direct) | 6–8 h | ✅ done (~6 h) — patch 0005 |
| 3: EPCS write+verify driver + read-back-skip | 6–8 h | 🟡 **firmware verified (cross-tests + apply chain), HW pending** — patch 0008.  Five JTAG-volatile flashes hit autonomous-reconfig (~500 ms post-boot, NOT rublock WD).  HW debug scheduled for dedicated session — needs scope on nCONFIG + openFPGALoader --verbose + qsf option-bit inspection. |
| 4: ALTREMOTE_UPDATE trigger + UART drain | ~~2–3 h~~ **4–8 h** | 🔴 **deferred** — Phase 4.1 proved direct-primitive wrapper can't disable running watchdog; needs megafunction rework.  See Phase 4 section + [[reference-rublock-complexity]]. |
| 5: Cross-repo build orchestrator | 2 h | ✅ done (~2 h) |
| 6.0: First-boot bring-up (KEY2 pull-up + LED polarity) | 1 h | ✅ done (~4 h, includes original-qsf-was-broken diagnosis + RTL-bypass rewrite) — patches 0006 (rewritten 2026-05-17 PM: RTL bypass `rstn_int <= por_cnt(3)`; original qsf `WEAK_PULL_UP_RESISTOR` on E16 doesn't compile on EP4CE10F17C8) + 0007 (LED polarity).  Stage2 banner + CLK + SDRAM PASS verified on iron 2026-05-17 with rewritten patch 0006. |
| 6: HW bench validation (SD + mode 't' on iron) | 3–4 h | pending — needs SD card + Phase 3/4 |
| **7: LutCodec C port + σ⁻¹ + CRC + IMEM bump** | **8–12 h** | ✅ done (~4 h) |
| **8: mode 'g' generator handler** (incl. edits.bin CRC-32 gate) | **4–6 h** | ✅ done (~3.5 h) — software path; EPCS-write steps 8-10 wait on Phase 3-4 |
| 9: EPCS slot rotation (high-frequency reflash) | 4–6 h | deferred — triggered by > 10×/day mode 'g' bench data |
| 4-future: Watchdog enable + EPCS boot-attempt counter | +2 h | deferred — lands with WATCHDOG_EN=1, after Phase 8 |
| Slack for Quartus fit / IMEM bump iterations | 4–6 h | — |

**Total**: ~38–53 engineer-hours of focused work (was 22–29 in plan v1
before Track B). Track B adds 12–18 h on top of Track A. Phase 2
(Quartus IP) and Phase 7 (LutCodec port) dominate; both require
careful cross-validation against existing Python codec output before
trusting on-chip results.

**Critical path**: Track A (Phases 1→2→3→4→6) can land first and
provide value (variant-loading from SD). Track B (Phases 7→8) lands
on top once Track A's EPCS+ALTREMOTE_UPDATE infrastructure is silicon-
validated. Phase 7 dev can happen in parallel with Phase 2-4 since
LutCodec C port is host-side until Phase 8 wires it in.
