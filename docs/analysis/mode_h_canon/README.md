# mode H canon ingest + consumer byte-identity gate (2026-05-31)

Consumes the Cyclone_CRAM_Mapper (`EP4CE6`) mode-H deliverables D1–D4 and
proves the **on-chip C codec + D2 canon table** reconstructs every Quartus
golden RBF byte-for-byte. This is the host-side acceptance gate the original
`docs/notes/cyclone_cram_mapper_targets_modeH.txt` contract required before
mode H may flash any asymmetric-mask edit.

Triggered by `docs/notes/cyclone_cram_mapper_modeH_reply_2026_05_31.txt`.

## The headline finding — "canon is a no-op" is FALSIFIED

The consumer codec (`lutcodec.c:lut_apply_with_canon`) and `plan.md` assumed
the canon-2input layer is a no-op at pin-friendly columns. The producer's
2026-05-31 reply overturned that, and this gate confirms it empirically:

| pass | σ⁻¹ (C codec) only | σ⁻¹ + D2 canon |
|------|--------------------|----------------|
| byte-identical to Quartus gold | **0 / 44** | **44 / 44** |

Canon carries a real per-position XOR delta (17–66 cells/pos, spanning
lab_cram + block_band). All 44 golds — including both `0xFFFF` extremes —
require canon for byte-identity. Negative control (drop 1 canon cell) →
diff_bytes>0, so the gate discriminates.

## This does NOT contradict the symmetric silicon validation

Two different goals, both true:

* **Byte-identity to Quartus's *output*** — needs canon (this gate, 0/44 → 44/44).
* **Runtime LE truth-table correctness** — for symmetric `{0x0000, 0xFFFF}`,
  σ⁻¹-only is silicon-validated (mode H @ (22,12,4), 2026-05-21): the chip's
  LE read only its 16 LUT data cells (H1); the canon cells Quartus *would*
  have set were fitter metadata, not read at runtime.

So canon cells are real bits Quartus emits, but for the symmetric runtime case
they are not load-bearing. Whether they are load-bearing at runtime for
**asymmetric** masks (H2) is still **untested** — see caveats.

## Files

```
tables/canon_2input_codec_table_d1pp.json   D2 — 14 positions × {4444,6996,DEAD(+FFFF×2)}, N=0
tables/ff_presence_avoid_addrs.json          D3 — FF-presence cells the dirty walker must never XOR
tables/rbf_safe_byte_ranges.md               D4 — safe (lo,hi) ranges + per-build variable bytes
upstream_ref/verify_canon_d1pp_table.py      producer's round-trip verifier (Python σ⁻¹) — provenance
upstream_ref/build_canon_d1pp_table.py       producer's table builder (needs Quartus + golds) — provenance
test_canon_vs_golden.py                      CONSUMER gate — C σ⁻¹ (via lutcodec.so) + canon vs golds
gate_run_20260531.log                        captured PASS run
```

D2 table md5 `4dc37fbab8828034acda1270b19fa5e5` (== EP4CE6 source at ingest).

## Running

The 21 MB golden RBF set (D1) is **not vendored**; the gate reads it from the
producer checkout (same convention as `host/test_lutcodec_c.py`):

```
EP4CE6_REPO=/home/test/EP4CE6 python3 test_canon_vs_golden.py --check-no-canon --negative-control
```

The gate compiles the verbatim on-chip C (`scratch_merge/sw/stage2_loader/
{lutcodec,lutcodec_data,crc16}.c`) into `lutcodec.so` via the canonical
`h1h2_silicon_test/host/build_lutcodec_so.sh`, so it tests the exact bytes the
NEORV32 would XOR — not a host-only reimplementation.

## Scope caveats (do NOT over-cite a green run)

1. **Round-trip, not silicon.** The table was mined as
   `canon := (gold ^ baseline) ^ predict_sram` from these same builds. Green =
   the C σ⁻¹ agrees with the producer reference on all 44 mined points + canon
   and CRC compose correctly. It is NOT a hold-out or a physics proof.
2. **Production-iron transfer is unproven.** Canon cells are often far-flung
   (lab_cram/block_band across ~280 KB of the file). They were mined from a
   *minimal one-LUT* design; XOR-ing them onto the *full production iron* .rbf
   is only safe where those cells share the same baseline state. This needs a
   static check against D3 + the dirty-bitmap walker before any asymmetric flash.
3. **Partial coverage.** Table is N=0 only, Y∈{2,8,14,17,21}, mask-classes
   {XOR (6996/DEAD), AND/OR-perm (4444), const (FFFF×2 positions)}. The
   silicon-validated (22,12,**4**) is NOT in the table. Full XPART generality
   (1152 pos × mask-class × N) is a producer scaling task (reply Part C #1).
4. **On-chip cannot hold this table.** 246 KB JSON does not fit NEORV32 IMEM;
   asymmetric mode H must therefore be **host-prepared** (.rbf assembled on the
   host with σ⁻¹+canon, mode H just stages it), not computed on-chip.

## Status / next

* ✅ Host byte-identity gate passes (this dir).
* ⛔ Asymmetric **silicon** validation — still gated: needs a covered,
  observable production-iron LE + caveat #2 static analysis + a flash + cold-boot.
* ↩ Codec comment correction (`lut_apply_with_canon` no-op text) made in the
  in-repo scratch_merge copy; fold into a neorv32_rot patch separately.
