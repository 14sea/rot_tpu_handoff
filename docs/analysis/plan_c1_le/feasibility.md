# Plan C-1 LE-feasibility — dedup-aware estimate + empirical confirmation

**Date:** 2026-05-20 21:30 (analytical) + 21:34 (empirical)  
**Target device:** EP4CE10F17C8 (AX301), 10,320 LE / 423,936 mbits / 46 DSP9  
**Sources:** copies of fit reports + RTL pulled from `neorv32_rot/` and
`neorv32_tpu/` into this dir; see [[feedback-copy-into-handoff-repo]].  
**Merged scratch build:** `scratch_merge/quartus/neorv32_demo.fit.summary`
(Quartus 21.1 Lite, full quartus_map + quartus_fit, 2 min wall time).

## Top-line: SILICON-VALIDATED (empirical 9,458 LE / 91.7 %)

**Status as of 2026-05-20 21:50**: merged C-1 .rbf flashed to AX301 EPCS
page 0; cold-boot clean; stability probe passed (5× mode Q + full mode P
EPCS write/verify + dispatcher recovery). Captures:
`docs/captures/uart_20260520_2147_c1_merged_coldboot.log` and
`uart_20260520_2149_c1_merged_*.log`.

| Resource  | Analytical projection | **Empirical merged** | Budget | Headroom |
|-----------|----------------------:|---------------------:|-------:|---------:|
| LE        | ~9,832 (95.3 %)       | **9,458 (91.7 %)**   | 10,320 | **862 LE** |
| Mem bits  | 332,800 (78.5 %)      | 332,800 (78.5 %)     | 423,936 | 91 K bits |
| DSP9      | 15 (32.6 %)           | **16 (34.8 %)**      | 46     | 30 |
| Pins      | 55 (31 %)             | 55 (31 %)            | 180    | — |

Analytical was conservative by **374 LE**. Quartus shared optimization
context across modules better than the standalone-sum-minus-dedup model
predicted: `wb_tpu_accel` itself shrank from 1,093 LE (standalone) to
**747 LE** in the merged design — partly explained by one extra DSP9
inferred (15→16, moving multiplier work out of fabric), partly by
better global synthesis decisions when the TPU sat next to RoT's
existing XBUS infrastructure.

### Per-module empirical (`scratch_merge/`)

| Module | Standalone RoT | Standalone TPU | Merged | Notes |
|---|---:|---:|---:|---|
| `neorv32_top` | 4,352 | 4,115 | 4,374 | one SoC, no duplication |
| `wb_altasmi_parallel` | 244 | — | 253 | +9 (negligible) |
| `wb_altremote_update` | 213 | — | 212 | −1 (negligible) |
| `wb_sdram_ctrl` | 288 | 281 | 279 | dedup confirmed |
| `wb_sha256` | 3,633 | — | 3,615 | −18 (negligible) |
| **`wb_tpu_accel`** | — | 1,093 | **747** | **−346 vs standalone** |
| **ax301_top total** | 8,729 | 5,431 | **9,458** | 91.7 % LE |

Memory bits unchanged (`wb_tpu_accel` contributes 0 M9K, as predicted —
its state is six register banks in the accelerator itself).

### Timing (sta) — at parity with existing RoT baseline

| Corner | RoT standalone | C-1 merged | Delta |
|---|---:|---:|---:|
| Slow 1200mV 85C Setup CLOCK slack | −1.187 ns | −1.245 ns | −0.058 ns |
| Slow 1200mV 85C Setup CLOCK TNS  | −15.182 ns | −16.737 ns | −1.555 ns |
| Slow 1200mV 0C Setup CLOCK slack | (similar) | −0.984 ns | (similar) |

Both designs technically fail setup at the worst corner (Slow 85 °C
1200 mV). RoT standalone has been silicon-validated at 50 MHz on AX301
across the project's iron sessions — i.e. the slack-negative figure at
the worst PVT corner has never been the practical limit. The TPU
integration degrades worst-case Setup TNS by ~1.5 ns and slack by
~58 ps — within noise of the existing accepted baseline. **No timing
regression load-bearing for C-1.** If a margin pass becomes necessary
later, `derive_clock_uncertainty` is missing from the SDC (Critical
Warning 332168), and standard fitter-seed sweeps are available.

| Resource  | RoT today   | + TPU peripheral | Projected   | EP4CE10 budget | Util |
|-----------|-------------|------------------|-------------|----------------|------|
| LE        | 8,729       | + ~1,103         | **~9,832**  | 10,320         | **95.3 %** |
| Mem bits  | 332,800     | + 0              | 332,800     | 423,936        | 78.5 % |
| M9K       | unchanged   | + 0              | (current)   | 46             | — |
| DSP9 mult | 0           | + 15             | 15          | 46             | 32.6 % |
| Pins      | 55          | + 0              | 55          | 180            | 31 %  |

Headroom: **488 LE** (4.7 %). Tight on LE, comfortable elsewhere. Router
congestion is the chief risk at ~95 %; trivial escape valves exist (see
"§ LE escape valves" below).

## Why the standalone sum is misleading

Standalone numbers (RoT 8,729 LE + TPU 5,431 LE = 14,160 LE = **137 %**)
double-count the NEORV32 SoC that both designs instantiate. Hierarchy
breakdown:

### TPU standalone (5,431 LE)

| Subtree                                | LE     | mbits   | M9K | DSP9 |
|----------------------------------------|--------|---------|-----|------|
| `neorv32_top` (its own CPU + I/O)      | 4,115  | 166,912 | (16)| 0    |
| `wb_sdram_ctrl` (SDRAM controller)     | 281    | 0       | 0   | 0    |
| **`wb_tpu_accel` (the actual TPU)**    | **1,093** | **0**| **0** | **15** |
| (top scaffolding)                      | ~-58   |         |     |      |

The "TPU" we want is just `wb_tpu_accel` — everything else is SoC
scaffolding RoT already provides. **DSP9 multipliers (15) live inside
the systolic array PEs** and are net-new (RoT today uses 0).

### RoT standalone (8,729 LE)

| Subtree                                | LE     | mbits   | Notes |
|----------------------------------------|--------|---------|-------|
| `neorv32_top` (CPU + caches + I/O)     | 4,352  | 332,800 | 8 KB DMEM + 32 KB IMEM ROM + caches + bootrom |
| `wb_sdram_ctrl`                        | 288    | 0       | |
| `wb_altasmi_parallel` (EPCS access)    | 244    | 0       | |
| `wb_altremote_update` (AS REMOTE IP)   | 213    | 0       | incl. 161 LE altremote_update IP |
| **`wb_sha256` (HW SHA accelerator)**   | **3,633** | **0** | **largest single block; candidate for removal in C-1** |

### Dedup math

```
Baseline:   RoT standalone                          = 8,729 LE
Add:        wb_tpu_accel + nested submodules        = 1,093 LE
Add:        new XBUS address-decode comparator      =    ~5 LE  (one F00-prefix compare)
Add:        possible LED-mux / debug routing growth =    ~5 LE
Subtract:   nothing (no overlap with existing RoT)
──────────────────────────────────────────────────────────────
Projected combined ax301_top                       ≈ 9,832 LE (95.3 %)
```

`wb_tpu_accel` is **Wishbone slave-only** (verified in
`wb_tpu_accel.v:7-23` — only one XBUS port, no master interface), so
integration is mechanical: drop in the three RTL files, add one address
range to the XBUS mux in `ax301_top.vhd:298-301`.

## Bus integration sketch

Current RoT XBUS address map (from `ax301_top.vhd:5-8, 298-301`):

```
0x40000000–0x41FFFFFF  SDRAM           (catch-all "else")
0xF10xxxxx             wb_sha256
0xF20xxxxx             wb_altasmi      (EPCS)
0xF30xxxxx             wb_altremote    (AS REMOTE)
```

TPU defaults to `0xF0000000–0xF000003F` per `wb_tpu_accel.v:5`. No
collision. Drop one line into `ax301_top.vhd`:

```vhdl
tpu_range <= '1' when xbus_adr(31 downto 20) = x"F00" else '0';
```

…and route `xbus_stb` / `xbus_cyc` to a fourth peripheral. The
multiplexer that already handles SHA/ASMI/REMOTE/SDRAM grows by one
case. Total RTL change is ~10 lines.

## RoT firmware impact

`wb_tpu_accel` has no DMA / no internal RAM — operands live in CPU
registers transferred via 32-bit XBUS stores. So:

- **No DMEM growth** required (TPU state is 6 registers in the
  accelerator itself; the CPU just writes them).
- **IMEM ROM growth** for TPU control code (LUT codec, weight loading,
  mode_g logic). Current RoT stage2 footprint < 32 KB; mode_g software
  is ≲ 4 KB based on EP4CE6 LutCodec C port. Should fit.
- **Trust anchor** remains the baked-in stage2 + (no separate TPU.rbf
  to hash anymore — TPU is *inside* the bitstream). This collapses
  several Phase 6 verification paths; see "§ Architecture simplifications".

## LE escape valves (in order of effort)

If the 95.3 % fit fails router or timing on the first try:

1. **Drop `wb_sha256`** → **-3,633 LE** (lands at ~62 %).
   Justification: in C-1 there's no separate `tpu.rbf` to hash before
   flashing EPCS page 1 (TPU is built-in). The remaining SHA use case
   is hashing Linux Image/initrd from SD before measurement — that's a
   Phase 6 nice-to-have, not load-bearing for CRTM. Software SHA-256
   in stage2 takes ~500 ms per 360 KB (plan §1929 item 5). **Strongly
   recommended for C-1.**
2. **Drop NEORV32 d-cache** → ~-300 LE (small CPI hit on data accesses)
3. **Drop NEORV32 i-cache** → ~-300 LE (small CPI hit on prefetch)
4. **Drop muldiv** (`Zmmul`) → ~-425 LE (stage2 doesn't use mul/div on
   hot path; software emulation OK)
5. **Drop bootrom internal** → ~-30 LE + frees ~32 K mem-bits
   (jump directly into IMEM ROM on reset)
6. **Drop atomic RMW/LR-SC** → ~-420 LE (no SMP in this design)

Combined items 1+4+6 frees 4,478 LE. Real headroom is enormous; the
95 % first-pass number is the *worst case*.

## Architecture simplifications enabled by C-1

The two-bitstream model required:
- `mode_t`: SD → SHA-verify TPU.rbf → EPCS page 1 write → trigger AS REMOTE
- `wb_altremote_update`: AS REMOTE config-controller drive
- AS REMOTE-enabling MSEL strap on the board (the current Phase 6 blocker)
- Multi-image POF tooling, write_param/data_in conditioning, etc.

C-1 deletes **all of the above** from the boot path:
- No second bitstream → no AS REMOTE → MSEL strap is no longer load-bearing.
- `wb_altremote_update` (213 LE) becomes dead code — can be removed entirely.
- `wb_altasmi_parallel` (244 LE) is still useful (EPCS access for
  measured boot of SD-resident Linux artifacts and for in-place
  RoT-firmware updates), but EPCS-write paths for TPU.rbf are gone.

Removing `wb_altremote_update` adds another **-213 LE** to the budget,
bringing first-pass projection to **~9,619 LE (93.2 %)** even before any
SHA-removal pass.

## Risks

1. **Router congestion at >90 % LE** is the historical Cyclone IV E
   pain point. EP4CE10 is small enough that placement gets tight even
   when timing closes. Mitigation: standard seed sweep, then escape
   valve #1.
2. **mode_g software complexity.** RoT must port the EP4CE6 LutCodec C
   code (sigma_inv table + predict_sram + bit-flip computation) and
   the in-RAM CRAM-cell driver. Roughly the same scope already
   estimated in plan §"Phase 7 Track B" (~8 h port, plus a host-side
   reference port for cross-test). Independent of LE budget.
3. **TPU's compute interface assumptions.** `tpu_accel.v` expects the
   CPU to push X (one 32-bit reg), 4 rows × 4 cols of int8 weights via
   bulk-load, then trigger compute (10-cycle latch). If C-1's mode_g
   needs different shape/precision, RTL changes may add LE. The
   existing TPU sized for 4×4 int8 MAC fits cleanly though.
4. **Pin contention with TPU's standalone top.** `neorv32_tpu`'s
   `ax301_top` reuses LED pins for `dbg_leds`. RoT's `ax301_top`
   already uses them. Pure decision at integration time — debug LEDs
   are non-load-bearing — no LE cost.

## Verdict and recommendation

**Plan C-1 is LE-feasible** with significant headroom once dead AS REMOTE
infrastructure is stripped and SHA goes to software. First-pass build
should aim for ~93 % LE (with `wb_altremote_update` removed,
`wb_sha256` retained as conservative baseline). If router fails, drop
SHA → ~62 % LE → very loose fit.

**Suggested next concrete step:** prepare a scratch dir under this
repo (`docs/analysis/plan_c1_le/scratch_merge/`) containing a copy of
RoT's `quartus/` + `rtl/` plus TPU's three RTL files, modify
`ax301_top.vhd` to add the TPU XBUS slave, run `quartus_map +
quartus_fit` and produce an empirical merged fit report. No sibling-
tree mutation; no flashing; ~30 min build time on Lite 21.1.

That gives us a real number to replace this estimate before committing
to firmware work.
