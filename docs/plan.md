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

### Phase 2 — ROT hardware: EPCS controller + ALTREMOTE_UPDATE

**Edits:**
- `/home/test/neorv32_rot/rtl/ax301_top.vhd`:
  1. Instantiate `altasmi_parallel` (Quartus megawizard → Cyclone IV → Active Serial Memory Interface). Drives the dedicated dual-purpose pins (`DCLK`, `DATA0`, `nCSO`, `ASDO` — automatic, no QSF pin assignments). Wrap in Wishbone slave at `0xF2000000` with regs `CMD` / `ADDR` / `DATA_FIFO` / `STATUS`. Reuse the same XBUS mux pattern as `wb_sha256` (see `ax301_top.vhd` ≈line 102+).
  2. Instantiate `altremote_update` (megawizard → Remote Update). Wishbone slave at `0xF3000000` with regs `PAGE_SEL` / `RECONFIG_TRIGGER`. Connect `regin/data_in/read_param/write_param/reset/clock` per Cyclone IV remote-update handbook.
- `/home/test/neorv32_rot/quartus/neorv32_demo.qsf`: add IP-generated Verilog files; set **Configuration scheme = Active Serial**, **Configuration mode = Remote**, **Configuration device = EPCS16**.

**Test**: `quartus_sh --flow compile` → check `fit.summary` LE count stays under 10,320 (jailbreak die) and timing closes at 50 MHz.

**Effort**: 6–8 h (mostly Quartus IP wizard + RTL plumbing).

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

### Phase 4 — ROT firmware: trigger ALTREMOTE_UPDATE

**Adds**:
- `epcs.c`: `void epcs_remote_reconfig(uint32_t page)` — writes `PAGE_SEL=1` then `RECONFIG_TRIGGER=1` to `0xF3000000`. After this MMIO write, `nCONFIG` pulses low and the FPGA self-reconfigures from EPCS page 1; firmware never returns. UART prints `[rot] handing off to TPU bitstream…\r\n` and waits for TX-empty **THEN an extra busy-loop pause** before triggering — the TX-empty flag asserts when the shift register loads, not when the last STOP bit hits the wire (~87 µs at 115200-8N1). Mirror the proven pattern in `mode_set_baud` (main.c:326-328): `while (!(uart_ctrl[0] & TX_EMPTY)) { } ; for (volatile int i = 0; i < 2000; i++) { }` — the busy-loop covers worst-case PHY drain across baud rates.

**Watchdog policy** (avoid infinite-reconfig loops):
- ALTREMOTE_UPDATE supports `WATCHDOG_EN` + `WATCHDOG_TIMEOUT`. If the loaded image (TPU) doesn't write to the watchdog clear register within ~timeout seconds of CONF_DONE, FPGA self-falls-back to page 0 (ROT). If ROT then re-runs mode_t and re-triggers, infinite reboot.
- **For initial bring-up: set `WATCHDOG_EN=0`** (manual fallback only). Removes the loop-trap class entirely.
- Future enablement (after TPU firmware grows a clear-routine): set `WATCHDOG_EN=1`, `WATCHDOG_TIMEOUT≥30s`, AND require TPU's stage2_tpu / Linux to clear the watchdog (write to ALTREMOTE_UPDATE's `update` register early in boot). Until then, leave disabled.

**Test**: scope `nCONFIG` pin (or just observe UART silence then TPU stage2 banner ~150 ms later → kernel boot). Verify no extra dots/garbage at end of UART trace from truncated final byte.

**Effort**: 2–3 h.

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

**New files in patches/neorv32_rot:**
- `sw/stage2_loader/lutcodec.c` + `lutcodec.h`:
  - `lut_codec_t lc_init(uint16_t x, uint16_t y, uint16_t n)` —
    computes the per-LE 16 minterm cell offsets via the same column-
    anchor + Y/N stride math as `LutCodec.from_cram_model`. Output:
    16 (off, bp) tuples cached in the struct.
  - `void lut_apply_mask(uint8_t *rbf, const lut_codec_t *lc, uint16_t mask)`
    — writes 16 bits absolutely (not XOR-delta) per the mask.
  - `int lut_apply_with_canon(uint8_t *rbf, uint16_t x, uint16_t y,
    uint16_t n, uint16_t mask)` — wraps the above + applies σ⁻¹
    canonicalization for asymmetric masks at left-edge columns
    (X∈{3,4,6,7}). For pin-friendly columns (X∈{10,16,22,28}), σ⁻¹
    is a no-op.
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

**Validation gate**: round-trip test against Python LutCodec.
`host/test_lutcodec_c.py`:
1. Generate 100 random `(x, y, n, mask)` tuples for valid CE6 LE
   positions.
2. For each, compute the modified `.rbf` via Python `LutCodec.write_tt`.
3. Compute the same modification via the C codec (cross-compile
   `lutcodec.c` for host x86_64, link as Python ctypes module).
4. Assert byte-identical RBF output.

**Memory budget on NEORV32**:
- IMEM 16 KB: stage2 currently 9.5 KB; +lutcodec ~3 KB code → ~12.5 KB.
  Tight but fits.
- σ⁻¹ + per-position tables: ~30 KB. Goes in `.rodata` of stage2,
  which lives in IMEM_INIT_FILE → bitstream. **OVERRUNS 16 KB IMEM**.
  Solution: bump IMEM_INIT_FILE to 32 KB (NEORV32 generic
  `MEM_INT_IMEM_SIZE`). Adds ~3.5k LE per NEORV32 docs (M9K-backed).
  Still fits on jailbreak die (10,320 LE total).
- SDRAM scratch: 2× 368 KB buffers (one for source, one for output of
  apply_lut). Existing 32 MB SDRAM more than enough.

**Effort**: 4–6 h (port) + 4–6 h (validation + IMEM bump) = ~8–12 h.

### Phase 8 — Track B: `mode_g` (generate) handler

Wire Phase 7 into stage2's dispatcher.

**New mode 'g' in main.c**:
```
mode_generate:
  1. Read base.rbf from SD region 0 → SDRAM[BASE_SCRATCH=0x41000000]   (~0.7s @ ~0.5 MB/s SD)
  2. Read edits.bin from SD (small, 4-32 KB)                           (negligible)
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

**Edit list format** (`edits.bin` on SD): packed array of 8-byte records:
```c
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

**Trust boundary**:
- Base bitstream: SHA-256-verified vs baked golden (high trust).
- Edit list: NOT SHA-verified vs baked golden — its purpose is to be
  application-mutable. Authentication is upstream of ROT (e.g., signed
  by user's NN compiler). For initial bring-up: no edit-list signature;
  add Ed25519 signature verify in a future phase if needed.
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

## Failure-mode handling matrix

| Failure | Detection | Action / UART |
|---|---|---|
| SD read error | `sd_read_many()` non-zero | `[!] SD read FAIL lba=…` halt |
| SDRAM scratch corrupted | pre-read pattern test (Phase 3 add) | `[!] SDRAM scratch test FAIL` halt |
| SD-side base SHA mismatch (Track A or B) | golden compare in mode_t / mode_g | `[!] base SHA mismatch — refusing EPCS write` halt |
| Edit position outside whitelist (Track B) | `editable_les.bin` allowlist check | `[!] edit (X,Y,N) not in editable region — halt` |
| Edit at unmapped CE6 position (Track B) | `lc_init` returns NULL | `[!] (X,Y,N) outside CE6 LE whitelist — halt` |
| Asymmetric mask at unmined σ⁻¹ position (Track B) | canon table KeyError-equivalent in C | `[!] σ⁻¹ not mined for (X,Y,N) — repin LUT to clean column` halt |
| EPCS already up-to-date | pre-erase region-1 hash match | `[rot] EPCS region 1 already matches — skipping write`, jump straight to reconfig |
| EPCS erase/program timeout | `WIP` poll exceeds N ms | `[!] EPCS WIP stuck — halt` |
| EPCS post-write SHA mismatch | second hash compare | `[!] EPCS verify failed — re-erasing region 1 to 0xFF` halt |
| ALTREMOTE_UPDATE error | altremote `current_state` ≠ idle after trigger | `[!] reconfig refused` halt in ROT |
| Target boots but hangs | watchdog in altremote_update (WATCHDOG_EN=1) | HW auto-fallback to page 0; ROT prints `[rot] FALLBACK` (initial bring-up keeps WATCHDOG_EN=0 to avoid loop-trap) |
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
| 2: Quartus IP (altasmi + altremote) | 6–8 h | pending — interactive Quartus session |
| 3: EPCS write+verify driver + read-back-skip | 6–8 h | pending — needs Phase 2 hardware |
| 4: ALTREMOTE_UPDATE trigger + UART drain | 2–3 h | pending |
| 5: Cross-repo build orchestrator | 2 h | ✅ done (~2 h) |
| 6: HW bench validation | 3–4 h | pending — needs AX301 + SD card |
| **7: LutCodec C port + σ⁻¹ + CRC + IMEM bump** | **8–12 h** | ✅ done (~4 h) |
| **8: mode 'g' generator handler** | **4–6 h** | **pending — Track B integration** |
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
