# H1/H2 silicon discrimination test — does σ⁻¹ math drive Cyclone IV LE runtime?

**Created**: 2026-05-21
**Status**: **ABANDONED 2026-05-21 evening** — see "Why abandoned" below.
**Disposition**: H1/H2 question deferred. mode H first-ship scope narrowed
to symmetric masks {0x0000, 0xFFFF}, where H1 vs H2 has no observable
behavioral difference. Asymmetric-mask scope gated until the underlying
toolchain limitation is resolved.

## Why abandoned

The entire test design assumes Quartus can lock a sentinel
`cycloneive_lcell_comb` instance at a specific `LCCOMB_X{x}_Y{y}_N{n}`
position so the host-side σ⁻¹ codec knows exactly which CRAM cells to
edit. During the build attempt 2026-05-21 evening, the team discovered
that **Quartus 21.1.0 Lite Edition silently drops all `LCCOMB_*` location
assignments** — both for this design and for the provider's known-good
EP4CE6/jailbreak/jbscan reference. See
[[feedback-quartus-lite-no-lccomb-lock]] for the empirical trail (h1h2
single LE, h1h2 16-LUT chain, jbscan remapped to codec-readable Y all
failed to place LEs at the locked positions).

Without LCCOMB locks the sentinel lands at an unknown, non-deterministic,
likely non-pin-friendly position, and the codec has no way to apply.
Path A "post-fit discovery" was explored and rejected (see session log
~19:40 2026-05-21): no working TCL API found in Lite to query
combinational LE placement, and bitstream-diff fallback fails because
Auto-Fit produces different-size bitstreams across builds with trivial
RTL changes.

## What survives

The host-side tooling built during this attempt is repurposed as
general-purpose codec validation infrastructure (no LCCOMB lock needed):

```
docs/analysis/h1h2_silicon_test/host/
├── build_lutcodec_so.sh       (gcc -shared from scratch_merge/sw/stage2_loader/)
├── lutcodec.so                (host ctypes wrapper around the on-chip C codec)
├── edit_rbf.py                (CLI: apply target mask at any (x,y,n) → patched .rbf)
└── scan_rbf.py                (CLI: dump LUT mask at any (x,y,n) or sweep the
                                 1152-position pin-friendly grid)
```

These tools were silicon-tested against existing RBFs and work correctly.
Future Path D (production-side mode g + asymmetric-mask H2 probe) reuses
edit_rbf.py / scan_rbf.py as-is.

The dead-end RTL + QSF were moved to
`docs/analysis/h1h2_silicon_test/abandoned_2026-05-21/` for archival
purposes; do not iterate on them.

---

(Original test design follows for historical reference; do NOT execute.)

---

## Context — why this test exists

Cyclone_CRAM_Mapper REPLY (`docs/notes/cyclone_cram_mapper_targets_modeH.REPLY.txt` +
`EP4CE6/results/golden_rbf_modeH/sweep_summary.json`) delivered D1 over-spec'd: 14
stratified positions × 2-4 masks = 32 Quartus builds, all with byte-level diff JSON.

Host-side analysis of the diffs (this session, 2026-05-21):

- Codec's predicted cell positions are CORRECT. For X10Y2N0 mask=0x6996, codec
  predicts 8 cells (popcount of mask) which form an EXACT subset of Quartus's
  72-cell mask=0 → mask=M diff. (|A∩B|=8, |A|=8, |B|=72 → A ⊂ B strict.)
- The 64 "extras" Quartus flips break down as:
  - 9 cells in block_band (1-input canon, header band, outside mode H edit range)
  - 12 cells below col base 0x13F00 (other-column routing fitter decisions)
  - 9 cells in lab_cram band near col base (some canon-2input + local routing)
  - 1 cell at ~0x59xxx (X=29 region, distant net re-routing)
- The "mismatch" against Quartus is purely Quartus's fitter making different
  routing + canon-state decisions when given a non-zero mask. These are
  compile-time decisions, not runtime LE cell content.

**Open question this test answers**:

- **H1**: Cyclone IV LE reads only the 16 LUT data cells at runtime. Canon-cells
  and routing-CRAM differ between Quartus mask=0 and mask=M builds, but those are
  fitter-only artifacts. mode H's XOR onto a mask=0 baseline produces a chip
  whose LE truth table = σ⁻¹(M). mode H plan proceeds as-written.

- **H2**: LE reads canon-cells (or routing inputs to LE are remapped by them) at
  runtime. mode H's σ⁻¹-only XOR onto mask=0 baseline produces a truth table
  ≠ M because canon-state assumed by σ⁻¹ is incompatible with whatever Quartus
  intended at mask=M. mode H plan needs to either narrow scope to symmetric
  masks (0x0000 / 0xFFFF only) or upstream must extend codec with per-position
  2-input canon cells (~60 hr provider effort).

This test is the cheapest possible silicon discriminator.

## Test design

Standalone test bitstream, completely isolated from the RoT/TPU production iron.
Built and flashed in `docs/analysis/h1h2_silicon_test/`. Three flashes total;
production bitstream restored from backup at the end.

### RTL: one LUT, KEYs in, LED out

`docs/analysis/h1h2_silicon_test/rtl/h1h2_top.v`:

```verilog
// One pin-friendly LUT exposing its full truth table on LED0 via KEY[3:0] sweep.
// mask=0x0000 baseline; host edits the EPCS .rbf to apply σ⁻¹(M) for test masks.

module h1h2_top (
    input  wire [3:0] key,        // 4 AX301 KEYs; ACTIVE-LOW
    output wire       led0        // AX301 LED0; ACTIVE-HIGH
);
    wire [3:0] keyn = ~key;       // invert so KEY pressed = 1

    cycloneive_lcell_comb #(
        .lut_mask        (16'h0000),
        .sum_lutc_input  ("datac")
    ) sentinel_lut (
        .dataa   (keyn[0]),
        .datab   (keyn[1]),
        .datac   (keyn[2]),
        .datad   (keyn[3]),
        .combout (led0)
    );
endmodule
```

### QSF: pin assignments + lock LUT location

`docs/analysis/h1h2_silicon_test/quartus/h1h2_top.qsf`:

```tcl
# Family + device + voltage standards lifted from neorv32_rot/quartus
set_global_assignment -name FAMILY "Cyclone IV E"
set_global_assignment -name DEVICE EP4CE10F17C8
set_global_assignment -name TOP_LEVEL_ENTITY h1h2_top
set_global_assignment -name VERILOG_FILE ../rtl/h1h2_top.v
set_global_assignment -name DEVICE_FILTER_PACKAGE FBGA
set_global_assignment -name DEVICE_FILTER_PIN_COUNT 256
set_global_assignment -name CYCLONEII_RESERVE_NCEO_AFTER_CONFIGURATION "USE AS REGULAR IO"

# AX301 pin map (per [[reference-ax301-board-quirks]])
set_location_assignment PIN_E1  -to key[0]
set_location_assignment PIN_M16 -to key[1]
set_location_assignment PIN_M15 -to key[2]
set_location_assignment PIN_E16 -to key[3]
set_location_assignment PIN_J13 -to led0    # confirm against actual pin map

set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to key[*]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to led0
set_instance_assignment -name WEAK_PULL_UP_RESISTOR ON -to key[*]

# LOCK the LUT to pin-friendly X10Y2N0
set_location_assignment LCCOMB_X10_Y2_N0 -to sentinel_lut

# Don't let the fitter merge or optimize away
set_instance_assignment -name DONT_MERGE_REGISTER ON -to sentinel_lut
set_global_assignment -name AUTO_SHIFT_REGISTER_RECOGNITION OFF
```

(KEY pin numbers and LED0 pin need to be sanity-checked against
[[reference-ax301-board-quirks]] before the build — that memory has the truth.)

### Build flow

```bash
cd docs/analysis/h1h2_silicon_test/quartus
quartus_map h1h2_top
quartus_fit h1h2_top
quartus_asm h1h2_top
# .rbf is in output_files/h1h2_top.rbf
# Verify LUT really did land at X10Y2N0:
grep -i "X10_Y2" h1h2_top.fit.rpt | head
```

### Host editor

`docs/analysis/h1h2_silicon_test/host/edit_rbf.py`:

```python
"""Apply lut_apply_mask to a baseline .rbf in-place via ctypes,
then patch frame CRCs. Pure host tool, no NEORV32 / no SD.

Usage:
    python3 edit_rbf.py baseline.rbf out.rbf --xyn 10,2,0 --mask 0xFFFF
"""
# ctypes-load scratch_merge/sw/stage2_loader/lutcodec.c + crc16.c
# (build a .so first: gcc -shared -fPIC -o lutcodec.so lutcodec.c lutcodec_data.c crc16.c)
# call lc_init, lut_apply_mask, lc_patch_rbf_crc_frames
# write out.rbf
```

Three target .rbf files to flash:

1. `h1h2_top.rbf` — Quartus baseline mask=0x0000 (LED expected always off)
2. `h1h2_mask_FFFF.rbf` — edit_rbf.py with mask=0xFFFF (LED expected always on)
3. `h1h2_mask_6996.rbf` — edit_rbf.py with mask=0x6996 (LED expected = XOR(KEY[3:0]))

## Test sequence (iron)

Listener-first per [[feedback-uart-listener-first]] not needed here — no UART
output, observation is via LED.

```
A. Backup current iron
   openFPGALoader --dump-flash ... epcs_backup_pre_H1H2_test_$(date).bin
   Verify md5 — should match epcs_backup_20260520_2255_pre_c1v4_flash.bin or
   the post-C-1-v4 state.

B. Baseline (mask=0x0000) flash + sweep
   openFPGALoader -f h1h2_top.rbf
   power cycle
   For each KEY[3:0] combination in {0..15}: press, observe LED, record.
   Expected: LED always off → TT = 0x0000.

C. Host-edit to mask=0xFFFF
   python3 edit_rbf.py h1h2_top.rbf h1h2_mask_FFFF.rbf --xyn 10,2,0 --mask 0xFFFF

D. Flash + sweep
   openFPGALoader -f h1h2_mask_FFFF.rbf
   power cycle
   Sweep all 16 KEY combos, record LED.

   - H1 outcome: LED always on → TT = 0xFFFF → σ⁻¹ math suffices for trivial mask
   - H2 outcome (strong): LED sometimes off → canon-cells affect runtime even for
     symmetric masks
   - H2 outcome (medium): LED always off → catastrophic; chip didn't even read
     our edited cells (CRC repair failed? wrong address?)

E. Host-edit to mask=0x6996
   python3 edit_rbf.py h1h2_top.rbf h1h2_mask_6996.rbf --xyn 10,2,0 --mask 0x6996

F. Flash + sweep
   Sweep all 16 KEY combos, record LED.

   0x6996 = 0110 1001 1001 0110, indexed by minterm 0..15:
   keyn[3:0] = 0  → LED = 0     (mask bit 0)
   keyn[3:0] = 1  → LED = 1     (mask bit 1)
   ... etc per mask bit positions

   - H1 outcome: observed truth table == 0x6996 (4-input XOR)
   - H2 outcome: observed != 0x6996 (could be a permuted version,
     suggesting canon-cells DO affect input mapping)
   - H1 partial: observed is a permutation of 0x6996 with stable pattern
     (suggesting σ⁻¹ table at this position needs swapping for which input
     is dataa/b/c/d, but the underlying math is right)

G. Restore production iron
   openFPGALoader -f epcs_backup_pre_H1H2_test_<date>.bin
   power cycle
   Verify stage2 banner comes up as expected (RoT/TPU C-1 v4 state).
```

## Outcome interpretation table

| Step D (0xFFFF) | Step F (0x6996) | Verdict | mode H plan action |
|---|---|---|---|
| LED always on | TT = 0x6996 | **H1 wins** | Proceed M0-M4 as written; M1 methodology drops "vs Quartus golden", keep "vs Python LutCodec sibling" |
| LED always on | TT = permuted 0x6996 | **H1 with input renaming** | mode H produces deterministic but input-permuted TT; sentinel docs note the permutation; still demonstrable as fabric surgery |
| LED on/off mixed | any | **H2 confirmed** | mode H scope shrinks to mask∈{0x0000, 0xFFFF}, OR upstream extends codec (D2 option B, ~60 hr) |
| LED always off (both steps) | LED always off | **codec/CRC bug** | mode H plan blocked; debug edit_rbf.py + frame CRC repair before any further work |

## Capture format

`docs/analysis/h1h2_silicon_test/captures/h1h2_sweep_<date>.md`:

```markdown
| Build         | KEY[3]=0 | 1 | ... | KEY[3]=15 | Observed TT | Expected | Match |
| baseline       | off     | off | ... | off       | 0x0000      | 0x0000   | ✓     |
| mask_FFFF      | on      | on  | ... | on        | 0xFFFF      | 0xFFFF   | ✓     |
| mask_6996      | off     | on  | ... | off       | 0x6996      | 0x6996   | ✓     |
```

## Cost estimate

- Verilog + QSF write + sanity-check pin assignments: 30 min
- Quartus compile (3 times if iterating): 5-15 min
- Build host .so + edit_rbf.py: 30-60 min (ctypes wrapping)
- Backup current iron: 5 min
- Three flashes + sweeps + capture: ~45 min
- Restore: 5 min

**Total: ~2-3 hr from blank repo state to verdict.**

## Files to be created next session (in rot_tpu_handoff, not provider repo)

```
docs/analysis/h1h2_silicon_test/
├── rtl/
│   └── h1h2_top.v                    (~25 lines)
├── quartus/
│   └── h1h2_top.qsf                  (~30 lines)
├── host/
│   ├── build_lutcodec_so.sh          (gcc -shared from scratch_merge/sw/stage2_loader/)
│   ├── edit_rbf.py                   (~80 lines)
│   └── lutcodec.so                   (built artifact)
└── captures/
    └── (post-experiment markdown)
```

Do NOT touch:
- /home/test/EP4CE6/ — provider's territory
- /home/test/neorv32_rot/ — production RTL, no sentinel patch yet per [[feedback-copy-into-handoff-repo]]
- /home/test/neorv32_tpu/ — same
- The current EPCS iron — only after full backup + after test concludes restore from backup

## Dependencies on existing code

| File | Reused for | Path |
|---|---|---|
| lutcodec.c, lutcodec.h, lutcodec_data.c, lutcodec_data.h | math | scratch_merge/sw/stage2_loader/ |
| crc16.c, crc16.h | frame CRC repair | scratch_merge/sw/stage2_loader/ |
| EPCS backups | restore-from-backup | docs/safety/epcs_backup_20260520_*.bin |
| Provider golden RBFs (informational, NOT for flash) | sanity-checking host edit_rbf.py output | EP4CE6/results/golden_rbf_modeH/ |
| AX301 pin map | KEY/LED pin numbers | [[reference-ax301-board-quirks]] |
