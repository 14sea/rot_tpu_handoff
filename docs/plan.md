# ROT → SD-staged TPU bitstream self-reconfig — feasibility plan

## Context

**Question**: are the bitstream RE work (`/home/test/EP4CE6/`) and the existing
NEORV32 projects (`neorv32_rot`, `neorv32_tpu`) sufficient to support a flow
where the FPGA boots ROT, ROT integrity-checks payloads (already done) AND
verifies a TPU bitstream loaded from SD card, then triggers FPGA self-
reconfiguration to bring up the TPU bitstream?

**Direct answer**:

- **The bitstream RE work is NOT what unblocks this** — Cyclone IV E lacks
  ICAP and has no CPU-side CRAM access path. The RE work's only
  contribution is the ζ pipeline's deterministic byte-identity gate, which
  lets ROT bake a stable `sha256(neorv32_tpu.rbf)` constant.
- **The actual mechanism is standard Altera vendor IP**: `altremote_update`
  (Cyclone IV "Remote Update" megafunction) + `altasmi_parallel` (EPCS
  read/write). ROT loads TPU bitstream from SD → SHA-256-verifies →
  writes to EPCS region 1 → triggers ALTREMOTE_UPDATE → FPGA cold-reboots
  from EPCS page 1 → TPU comes up.
- **Feasible on EP4CE6 jailbreak die (10,320 LE)**: ROT currently sits at
  ~5.5k LE, plus +700–1,150 LE for the new IP — fits with ~2k slack on
  the EP4CE10-equivalent fabric. Tight on the nominal EP4CE6 6,272 LE
  budget; trim list documented below.
- The user explicitly said "計劃中不用管備忘錄中阻止" — this plan ignores
  the memo'd open-toolchain routing density blocker (only relevant if
  pursuing the alternative single-bitstream MMIO-mode-switch design,
  which we are NOT).

## Architecture

```
SD card (FAT-or-raw blob with TPU.rbf at LBA 0x10000)
    │  (ROT reads via existing wb_spi → sd_read_many)
    ▼
SDRAM scratch @ 0x41000000  ─── sha256_hw → compare to rot_golden_tpu_bitstream
    │  (verified)
    ▼
EPCS via new wb_altasmi (0xF2000000)  ── erase region 1 (0x80000) → program → read back → re-verify
    │  (post-write hash also matches)
    ▼
wb_altremote_update (0xF3000000)  ── PAGE_SEL=1, RECONFIG_TRIGGER=1
    │  (nCONFIG pulses, FPGA reboots)
    ▼
TPU bitstream (EPCS page 1) loaded by configuration controller
    │
    ▼
NEORV32 + TPU + Linux + /dev/npu live
```

## Implementation phases (existing patterns reused)

### Phase 1 — ROT firmware: SD-side TPU bitstream verify (no EPCS)

**Edits:**
- `/home/test/neorv32_rot/sw/stage2_loader/main.c`: add `mode_t` (TPU verify) handler — mirrors `mode_sd_boot` (lines 364–452). Reads `tpu.rbf` from new SD region (LBA 0x10000, 720 sectors = 368,640 B) into SDRAM scratch at `0x41000000`, calls existing `sha256_hw()` (`sha256_hw.h`), compares to new `rot_golden_tpu_bitstream[8]` constant.
- `/home/test/neorv32_rot/sw/stage2_loader/rot_golden.h`: add 4th 8-word constant.
- `/home/test/neorv32_rot/host/sd_layout.py` + `sd_pack.py`: extend header with `tpu_rbf_lba` / `tpu_rbf_sz` (4th payload).

**Test**: dispatcher mode `t` prints `[rot] TPU sha256=… [OK]` then halts before any EPCS write. Tamper test: flip a byte on SD → `[MISMATCH]` halt.

**Effort**: 3–4 h.

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

### Phase 5 — Build automation: bake TPU hash into ROT

**Edits:**
- `/home/test/neorv32_rot/host/gen_golden.py`: add fourth section `("TPU_RBF", "rot_golden_tpu_bitstream", "neorv32_tpu.rbf")`, path resolved via env var `TPU_RBF_PATH` (default `/home/test/neorv32_tpu/neorv32_tpu.rbf`). Reuses existing `sd_layout.sha256_for_card()` little-endian word packing — same convention as the other three goldens, so `sha256_hw()` comparison just works.
- New top-level `Makefile` (or extend existing) target `tpu-then-rot`:
  1. `cd /home/test/neorv32_tpu && make bitstream` → `python3 /home/test/EP4CE6/scripts/bit_workaround/zeta_pipeline.py …` → deterministic `neorv32_tpu.rbf`.
  2. `cp neorv32_tpu/neorv32_tpu.rbf neorv32_rot/output/tpu.rbf`.
  3. `cd /home/test/neorv32_rot/sw/stage2_loader && make` (triggers `gen_golden.py` → fresh `rot_golden_tpu_bitstream`).
  4. `cd /home/test/neorv32_rot && make bitstream` → `python3 …/zeta_pipeline.py` → ROT bitstream with embedded golden hash.

**Test**: `make tpu-then-rot && diff <(sha256sum neorv32_tpu/neorv32_tpu.rbf) <(grep rot_golden_tpu rot_golden.h)` (transformed).

**Effort**: 2 h.

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

## Failure-mode handling matrix

| Failure | Detection | Action / UART |
|---|---|---|
| SD read error | `sd_read_many()` non-zero | `[!] SD read FAIL lba=…` halt |
| SDRAM scratch corrupted | pre-read pattern test (Phase 3 add) | `[!] SDRAM scratch test FAIL` halt |
| SD-side SHA mismatch | golden compare in mode_t | `[!] TPU SHA mismatch — refusing EPCS write` halt |
| EPCS already up-to-date | pre-erase region-1 hash match | `[rot] EPCS region 1 already matches — skipping write`, jump straight to reconfig |
| EPCS erase/program timeout | `WIP` poll exceeds N ms | `[!] EPCS WIP stuck — halt` |
| EPCS post-write SHA mismatch | second hash compare | `[!] EPCS verify failed — re-erasing region 1 to 0xFF` halt |
| ALTREMOTE_UPDATE error | altremote `current_state` ≠ idle after trigger | `[!] reconfig refused` halt in ROT |
| TPU boots but hangs | watchdog in altremote_update (WATCHDOG_EN=1) | HW auto-fallback to page 0; ROT prints `[rot] FALLBACK` (initial bring-up keeps WATCHDOG_EN=0 to avoid loop-trap) |
| Truncated final UART byte | terminal sees garbage | TX-empty wait + busy-loop drain pause before nCONFIG (Phase 4) |

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

**What the open toolchain DOES contribute** (already in the plan via
Phase 5 ζ gates):
- ζ pipeline (`/home/test/EP4CE6/scripts/bit_workaround/zeta_pipeline.py`)
  validates byte-identity of Quartus output → ROT.rbf is provably
  deterministic across rebuilds → `rot_golden_tpu_bitstream` is a
  stable trust anchor.
- ζ regression corpus (`zeta_regression.py`) protects against codec
  drift in the larger EP4CE6 RE work that this project depends on.
- The `--canon-2input-unique-aware` etc. work-in-progress (P5b/P5c) is
  irrelevant to this project — it targets cross-LAB σ⁻¹ closure for
  small designs, not vendor-IP-heavy SoCs.

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

## Effort estimate

~22–29 engineer-hours of focused work — Phase 2 (Quartus IP integration
+ LE-budget juggling) and Phase 3 (EPCS driver bring-up with on-board
verify) dominate at ~6–8 h each; Phase 1 and Phase 5 are mostly
mechanical extensions of existing patterns (3–4 h and 2 h); Phase 4 is
small (2–3 h) but requires careful UART-drain ordering before the
nCONFIG pulse; Phase 6 bench validation adds another 3–4 h. Add ~4–6 h
slack for the first Quartus fit failure when the new IP pushes ROT past
10,320 LE — the trim list above is the escape hatch.
