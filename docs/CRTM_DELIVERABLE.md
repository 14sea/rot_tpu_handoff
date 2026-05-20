# Self-Reconfiguring CRTM on Cyclone IV E — Silicon-Validated Deliverable

**Date**: 2026-05-20  
**Board**: ALINX AX301 (EP4CE10F17C8, 10,320 LE / 423,936 mem-bits)  
**Status**: silicon-validated end-to-end, four progressively richer demos  
**Repos**: `neorv32_rot/`, `neorv32_tpu/`, orchestrator `rot_tpu_handoff/`

---

## 1. What this project proves

A trust-anchored **dynamic-loading CRTM** that:

1. Boots from an immutable bitstream-resident anchor (RoT firmware in IMEM ROM).
2. Reads structured payloads from mutable external storage (SD card).
3. Validates every payload through cryptographic integrity (CRC-32) **and**
   position-whitelist authorization (LutCodec `lc_init`).
4. Writes verified data into on-chip TPU LUT memory at runtime — no FPGA
   reconfiguration, no host PC mediation, no external trust extension.
5. Optionally replays Phase 7's σ⁻¹ permutation math per record, so the
   project's novel bit-flip-surgery contribution remains load-bearing
   even though no .rbf is being modified.

Every step above has been demonstrated on real iron and captured in
`docs/captures/uart_*.log`.

## 2. Iron evidence

| Demo | Capture | What it proves |
|---|---|---|
| Cold-boot probe | `uart_20260520_2149_c1_merged_probe.log` + `uart_20260520_2150_c1_merged_modeQ.log` | Stage2 dispatcher responds correctly after the v1 cold-boot.  (The `_2147_coldboot.log` listener-only capture is 0 bytes — USB PL2303 re-enumerated during power-cycle so the boot banner went to a dead fd; the probe captures are the load-bearing evidence.) |
| Stability sweep | `uart_20260520_2153_c1_merged_stability.log` | 5× mode Q + full mode P EPCS write/verify cycle + garbage-byte dispatcher recovery; SHA / ASMI / ALTREMOTE / TPU all coexist on the XBUS mux. |
| Single-record load | `uart_20260520_2218_c1v2_modeg_first.log` | mode_g reads 1 record from SD, gates via CRC + whitelist, writes to TPU LUT, reads back. |
| Idempotency | `uart_20260520_2219_c1v2_modeg_idempotent.log` | Two consecutive mode_g invocations produce identical successful traces. |
| 256-record load | `uart_20260520_2238_c1v2_modeg_256.log` | Full LUT-memory capacity (256 × 32-bit M9K) exercised; all 256 records survive the trust gates and persist. |
| Compute path | `uart_20260520_2250_c1v3_g_then_c.log` | mode_c auto-loads PE weights from LUT[0..15] and computes; results = Σ(x_k · LUT_byte_k_row) byte-match Python compute. |
| σ⁻¹ surgery | `uart_20260520_2302_c1v4_g_vs_G.log` | mode_G applies Phase 7 (pair, delta) math per record; silicon RES values byte-match Python σ⁻¹ reference (RES[0]=-66 / -66 / -322 / -322) across 4 rows. |

## 3. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  EP4CE10F17C8 (9,501 LE used / 92 %, 1 M9K added, 16 DSP9)         │
│                                                                    │
│  ┌──────────────────────────────────────────┐                      │
│  │  RoT NEORV32 SoC (RV32IMAC + caches)     │                      │
│  │   ┌─────────────────────────────────────┐│                      │
│  │   │ IMEM ROM 32 KB (stage2 firmware,    ││  ← immutable trust   │
│  │   │   baked into bitstream)             ││    anchor: every     │
│  │   │   • mode_g, mode_G, mode_c, mode_t, ││    boot loads the    │
│  │   │     mode_P, mode_X, mode_Q, ...     ││    same bytes        │
│  │   │   • rot_golden_* SHA-256 constants  ││                      │
│  │   │   • lutcodec_sigma_inv_packed table ││                      │
│  │   └─────────────────────────────────────┘│                      │
│  │   DMEM RAM 8 KB                          │                      │
│  │   D-cache + I-cache (2 × 2 KB)           │                      │
│  └────────┬─────────────────────────────────┘                      │
│           │ XBUS (Wishbone)                                        │
│           │                                                        │
│  ┌────────┼──────────────────────────────────────┐                 │
│  │ XBUS   ▼ address-decoded peripherals          │                 │
│  │  ├─ 0xF00xxxxx  wb_tpu_accel  (THIS PROJECT)  │                 │
│  │  ├─ 0xF10xxxxx  wb_sha256                     │                 │
│  │  ├─ 0xF20xxxxx  wb_altasmi_parallel (EPCS)    │                 │
│  │  ├─ 0xF30xxxxx  wb_altremote_update (legacy)  │                 │
│  │  └─ 0x40000000  wb_sdram_ctrl  (SDRAM 32 MB)  │                 │
│  └───────────────────────────────────────────────┘                 │
│                                                                    │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │ wb_tpu_accel  (Plan C-1 v3, 749 LE + 1 M9K + 16 DSP9)      │    │
│  │  ┌─────────────────────────────┐  ┌─────────────────────┐  │    │
│  │  │ Systolic 4×4 array          │  │ LUT memory          │  │    │
│  │  │  • 16 PEs (int8 × int8)     │  │  256 × 32-bit M9K   │  │    │
│  │  │  • CTRL[0] = compute        │  │  R/W via XBUS @     │  │    │
│  │  │  • CTRL[8] = auto-load      │◄─┼─ 0xF0000040-0xF000043F  │    │
│  │  │    weights from LUT[0..15]  │  │                     │  │    │
│  │  └─────────────────────────────┘  └─────────────────────┘  │    │
│  └────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────┘
                          ▲                       ▲
                          │ SPI                   │ JTAG (write-once
                          │                       │  per design rev)
                ┌─────────┴──────────┐     ┌──────┴──────┐
                │ SD card            │     │ EPCS (ST M25P16, 2 MB) │
                │ • header @LBA 0    │     │  page 0: this bitstream │
                │ • Image,DTB,initrd │     │   (RoT + TPU integrated)│
                │ • edits.bin @8729  │ ──→ │                         │
                │   (mutable, signed │     │  page 1: unused (legacy │
                │    upstream by NN  │     │   AS REMOTE slot)       │
                │    compiler)       │     │                         │
                └────────────────────┘     └─────────────────────────┘
```

## 4. Trust chain (the CRTM property)

Every bit that ends up driving TPU compute traces back through this chain:

```
1.  Bitstream pre-burn       → EPCS page 0 (sha-anchored at JTAG time;
                                bitstream contains RoT IMEM ROM)
2.  Cold-boot                → AS controller loads bitstream into config
                                SRAM; RoT NEORV32 wakes
3.  stage2.text + .rodata    → already in IMEM ROM (bit-identical to
                                bitstream build artifacts)
4.  edits.bin from SD        → mode_g/G reads bytes raw
5.  Magic + version gate     → reject anything not "EDIT" v1
6.  CRC-32 gate              → reject any in-transit corruption
7.  Per-record lc_init       → reject any (x,y,n) outside CE6
                                pin-friendly whitelist (Phase 7)
8.  (mode G only) σ⁻¹ math   → compute Phase 7-spec cell pattern
9.  Verified write           → packed 32-bit value to TPU LUT memory
10. Readback verify          → halt on any mismatch
11. mode_c CTRL[8] = 1       → TPU FSM reads LUT, drives PE weights
12. Compute runs on weights  → result depends ONLY on what survived
                                steps 5-7 (and step 8 in mode G)
```

Failure modes systematically closed:
- SD bit-flip in record → CRC catches (step 6).
- SD bit-flip in header → magic/version catches (step 5).
- Truncated SD → length check catches (step 6 prelude).
- Adversarial (x,y,n) outside whitelist → lc_init rejects (step 7).
- LUT memory R/W bug → readback verify catches (step 10).
- TPU compute drives un-loaded weights → CTRL[8] FSM idempotent;
  rerun mode_c reproduces results bit-perfect.

What this chain explicitly does NOT close (and isn't trying to):
- **Adversarial edits.bin authored upstream with valid CRC**: CRC
  detects accidental corruption, not authentication. Mitigation:
  signed (Ed25519) edits.bin upstream of RoT, public key baked into
  IMEM ROM `.rodata`. Out of scope for this deliverable.
- **EPCS reflash by an attacker with physical JTAG access**: project
  scope assumes the operator controls JTAG. CRTM rests on this.

## 5. LE / memory budget

| Resource | Cost (final v4 build) | Budget (EP4CE10F17C8) | Util |
|---|---:|---:|---:|
| LE | 9,501 | 10,320 | 92.1 % |
| Mem bits | 340,992 | 423,936 | 80.4 % |
| DSP9 | 16 | 46 | 34.8 % |
| Pins | 55 | 180 | 30.6 % |

Net cost of this project's contributions over baseline RoT (8,729 LE):
- TPU peripheral (systolic + LUT memory): +749 LE
- LUT-driven weight-load FSM: +52 LE  
- XBUS mux extension: ~10 LE
- 1 M9K block (LUT memory, 8,192 mem-bits)
- 16 DSP9 (systolic array PE multipliers)

Headroom: 819 LE remaining (7.9 %).

## 6. Architectural pivot context

The original Phase 6 plan called for two-bitstream architecture:
RoT.rbf in EPCS page 0, TPU.rbf in EPCS page 1, AS REMOTE reconfig to
swap between them. After all three software fixes landed
(Path B megafunction switch + multi-image POF generation +
`STRATIXIII_UPDATE_MODE=REMOTE`), the chip still rejected slot-1
reconfig with nSTATUS asserted. Root cause: AX301 MSEL[2:0] pin straps
likely select a non-REMOTE AS configuration mode, which is a board-
level hardware constraint that cannot be worked around in software.

Plan C-1 (this deliverable) eliminates the dependency on AS REMOTE
entirely. The MSEL strap blocker becomes moot. As a side effect:
- `wb_altremote_update` (212 LE) is now dead code, removable.
- `wb_altasmi_parallel` (253 LE) usage shrinks to EPCS self-update
  and `mode_t` SD-bitstream-verify only — both retained as optional
  features but not on the CRTM critical path.
- `wb_sha256` (3,615 LE) used by `mode_t` for SD-resident artifact
  verification; not load-bearing for the mode_g/G dynamic-loading
  chain (CRC-32 is the gate there) but kept for general measured-boot
  applications.

## 7. Phase mapping (against `docs/plan.md`)

| Phase | Title | Status under C-1 |
|---|---|---|
| 1 | SD-side TPU bitstream verify (mode_t) | ✅ DONE — silicon |
| 2 | EPCS controller + ALTREMOTE_UPDATE | ✅ DONE |
| 3 | EPCS write driver + verify (mode_t EPCS path) | ✅ DONE — silicon 2026-05-19 |
| 4 | Trigger ALTREMOTE_UPDATE | ✅ DONE — silicon 2026-05-17 |
| 5 | Build automation (orchestrator) | ✅ DONE |
| 6 | HW validation, AS REMOTE end-to-end | 🟡 → superseded by C-1; HW blocker (MSEL) no longer load-bearing |
| 7 | LutCodec C port + σ⁻¹ + CRC | ✅ DONE — exercised by mode_G v4 |
| 8 | mode_g (LUT mask editor) | ✅ DONE — reinterpreted as mode_g/G writing to TPU LUT memory instead of .rbf surgery + AS REMOTE |
| 9 | EPCS slot rotation | moot under C-1 (single bitstream, no high-frequency EPCS writes on mode_g path) |
| C-1 v2/v3/v4 | Integrated bitstream + LUT + σ⁻¹ replay | ✅ silicon-validated 2026-05-20 |

## 8. Patches (orchestrator chain, applied 2026-05-20)

This work folds into `rot_tpu_handoff`'s patch series:

- `patches/neorv32_tpu/0004` — `wb_tpu_accel` + `tpu_accel` v2 LUT memory + v3 LUT→weight FSM (RTL)
- `patches/neorv32_rot/0041` — `ax301_top.vhd` XBUS mux extension (TPU @ 0xF00xxxxx)
- `patches/neorv32_rot/0042` — `mode_generate` Plan C-1 v2 refactor (raw mask path)
- `patches/neorv32_rot/0043` — `.qsf` cross-repo VERILOG_FILE refs to `../../neorv32_tpu/rtl/*.v`
- (`mode_compute` v3 and `mode_generate_classic` v4 are in `scratch_merge/sw/stage2_loader/main.c`
  but not yet folded as separate patches — folding TBD per how the user wants the patch granularity)

Apply via `make apply-patches` from `rot_tpu_handoff/` root. Post-apply
sibling tips: `neorv32_rot` = `563dffb`, `neorv32_tpu` = `ac98ec7`.

## 9. Future hardening (out of scope for this deliverable)

- **Ed25519 signature on edits.bin**: replace CRC-32 (integrity only) with
  signed integrity; public key in IMEM ROM `.rodata`.
- **TPU LUT memory expansion**: 256 entries used for demo. Cyclone IV E's
  remaining 20 free M9K blocks could host up to 5,120 entries × 32 bit
  with the same XBUS slave + dual-port-read pattern.
- **TPU systolic shape parameterization**: current 4×4 int8. RTL is
  small enough to refit for 8×8 or 4×4 int16; LE budget headroom would
  shift accordingly.
- **`editable_les.bin` allowlist sidecar** (per plan §Phase 8 future-hardening): per-base bitstream allowlist of editable LE positions, cross-checked
  against each record alongside the static lc_init whitelist.
- **Linux/FreeRTOS workload that consumes TPU outputs**: orthogonal to
  the CRTM proof but would close a more application-shaped story.
