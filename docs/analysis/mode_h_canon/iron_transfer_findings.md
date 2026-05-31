# Production-iron transfer static analysis (caveat #2) — 2026-05-31

**Question**: can an asymmetric mode-H edit (σ⁻¹ + D2 canon, mined from a
*minimal one-LUT* design) be XOR-applied onto the *full production iron* .rbf?

**Verdict: NO — the minimal-design canon delta is not a transferable per-LE
patch. 0/44 mined (pos,mask) transfer cleanly onto production iron.**

Inputs: production iron = `bitrev(epcs_backup_20260521_pre_modeH.bin[:368011])`
(page-0 .rbf md5 `fd884770`, Plan C-1 v4, NO mode H — the authoritative iron
state); golds + D2/D3/D4 as in this dir. Tools: `analyze_iron_transfer.py`,
run log `iron_transfer_run_20260531.log`. No flash.

## Method

mode H's net change is `delta = predict_sram(M) △ canon[pos][M]` (symmetric
difference), which by construction == `gold_maskM XOR gold_mask0` on the
touched cells. So `production_new = production XOR delta`. The **necessary**
safety gate is a collision check:

> for every touched cell, `production[cell] == gold_mask0[cell]`
> (production must already be in the minimal-design mask=0 baseline state).

A collision (production ≠ baseline at a touched cell) means that cell is
occupied by other production logic → XOR-flipping it corrupts that logic.

## Findings

### 1. 13 of 14 covered positions are already occupied in production iron
Only `X16Y14N0` reads `mask=0x0000` with its 16 data cells matching the
baseline. The other 13 covered `(x,y,0)` positions hold real production logic
(e.g. X22Y2N0=0xFFDF, X16Y8N0=0xF0CA, X28Y17N0=0xFEFE) — they cannot be edited
at all without destroying function. The D2 whitelist (low-Y, N=0) largely
coincides with LEs a 92%-full design fills.

### 2. 0/44 (pos,mask) transfer cleanly — every one collides
Collision counts range 1–19 cells. Even the single free position
`X16Y14N0` collides: 0x4444→1, 0x6996→2, 0xDEAD→2 — all in **canon** cells
(its 16 data cells are clean). So canon, not σ⁻¹, is what fails to transfer.

### 3. Canon cells are shared / block-level, not per-LE
Across the 14 positions, **74/692 (10%) canon cells are shared by >1
position**; region split of shared cells: 48 lab_cram + 26 block_band. The
worst offenders:

| cell | region | appears in |
|------|--------|-----------|
| `0x5967B.2` | lab_cram | **all 14 positions** |
| `0x588E7.3` | block_band | 11 |
| `0x5898B.4` | block_band | 9 |
| `0x588D3.3` / `0x588D7.3` / `0x5898D.4` | block_band | 8 each |

These encode **block-level fitter state**, not the target LE's truth table.
Production iron populates them for its own logic, so they collide.

### 4. Canon cells are spatially non-local
Deep-dive `X16Y14N0 0x4444` (base `0x02BFCF`): of 29 Δ cells, only **9 lie
within the LE's 8-frame window (±1680 B)**; the rest reach **885 frames
(186 KB) away**, clustered in the block_band region near `0x58xxx`. A
context-independent per-LE patch would be local; this is not.

(D3 FF-avoid is a positive certificate — Cyclone IV has no per-LE FF-presence
bit — but D3's reasoning covered only the 16 minterm cells, *not* these canon
cells, so it does not bless the transfer.)

## What this means for asymmetric mode H

The D2 canon table is a faithful **byte-identity oracle for the minimal
design** (host gate: 44/44) but is **NOT a portable per-LE patch** for
production-iron surgery. "Apply canon for any new (pos,mask)" — the producer's
advice — is correct for matching Quartus's *minimal* build, but matching a
minimal build is neither achievable nor relevant on a populated production .rbf.

The viable path is therefore **σ⁻¹-only**, which *does* transfer cleanly:

* At the one free position `X16Y14N0`, the 16 σ⁻¹ data cells match the
  baseline (0 collisions); dropping canon makes the whole edit collision-free.
* This works **iff H1 generalises to asymmetric masks** — i.e. the runtime LE
  reads only its 16 LUT data cells (silicon-validated for symmetric
  {0x0000,0xFFFF} on 2026-05-21, **untested for asymmetric**). If instead H2
  holds for asymmetric (LE reads canon at runtime), asymmetric mode H on a
  populated iron is effectively infeasible via this RE approach, because the
  needed canon cells are shared/occupied (findings 2–4).

So the asymmetric question reduces back to the **original H1/H2 discriminator**
— not to canon transfer. Resolving it needs a silicon test of an asymmetric
mask at an *observable* LE, still gated by:
* the Quartus-Lite LCCOMB-lock blocker (can't place a sentinel), or
* a Path-D observation at a free, observable production LE. `X16Y14N0` is free
  but is an unused LE driving nothing observable — it would need RTL wiring to
  an XBUS/LED readout first.

## Bottom line

Caveat #2 is now quantified and **confirmed as a hard blocker** for the
canon-transfer approach. The host byte-identity gate (44/44) stands as a codec
correctness result; it does **not** imply production-iron asymmetric surgery is
ready. Next real step for asymmetric is the H1-for-asymmetric silicon test
(σ⁻¹-only at an observable LE), not canon ingestion.
