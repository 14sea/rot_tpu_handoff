# rot_tpu_handoff

Cross-repo orchestration for a **self-reconfiguring CRTM (Continuous
Root of Trust Measurement)** demo on ALINX AX301 (Cyclone IV E
EP4CE10F17C8 — *not* EP4CE6 despite the legacy naming).

**Status (2026-05-20): silicon-validated end-to-end.**  See
[`docs/CRTM_DELIVERABLE.md`](docs/CRTM_DELIVERABLE.md) for the formal
write-up; this README is the navigation layer.

## What was proven

A trust-anchored dynamic-loading chain, validated on iron across four
progressive demos:

| Demo | Mechanism | Iron evidence |
|---|---|---|
| **v1** cold-boot + stability | Merged single-bitstream (RoT + TPU peripheral) cold-boots cleanly; SHA / ASMI / ALTREMOTE / TPU coexist on the XBUS mux | `docs/captures/uart_*_2153_stability.log` |
| **v2** SD → TPU LUT load | `mode_g`: SD → CRC-32 gate → LutCodec `lc_init` whitelist → write to TPU on-chip LUT memory (256 × 32-bit M9K @ `0xF0000040`) → readback verify | `docs/captures/uart_*_2238_modeg_256.log` |
| **v3** LUT-driven compute | `mode_c`: `CTRL[8]=1` triggers an FSM that auto-loads PE weights from LUT[0..15] bits [23:16] → systolic compute on `X_IN={1,2,3,4}` → results = Σ(x_k · w_k_row), byte-matches Python | `docs/captures/uart_*_2250_g_then_c.log` |
| **v4** σ⁻¹ surgery preserved | `mode_G`: same SD→CRC→whitelist gate, but replays Phase 7 `lut_apply_mask` (pair, delta) math per record and stores σ⁻¹(mask) in the LUT; silicon RES rows byte-match Python σ⁻¹ reference | `docs/captures/uart_*_2302_g_vs_G.log` |

No FPGA reconfiguration, no AS REMOTE, no MSEL strap dependency.  The
project's novel σ⁻¹ + canon-2input + per-bit cell-mapping math from
Phase 7 is exercised end-to-end in `mode_G` without ever touching an
`.rbf` file.

## Layout

```
~/                          (parent of all repos)
├── neorv32_rot/            (sibling; patched on demand)
├── neorv32_tpu/            (sibling; patched on demand)
├── EP4CE6/                 (sibling; RE work, consulted read-only)
└── rot_tpu_handoff/        (this repo)
    ├── README.md
    ├── Makefile            (cross-repo build + apply/unapply orchestrator)
    ├── patches/
    │   ├── neorv32_rot/    (45 patches: Phase 1-8 + Plan C-1 v2-v4)
    │   └── neorv32_tpu/    (5 patches: Phase 6 REMOTE qsf + Plan C-1 v2-v3)
    └── docs/
        ├── plan.md         (full multi-phase plan with pivot history)
        ├── CRTM_DELIVERABLE.md  (formal proof writeup — start here)
        ├── analysis/plan_c1_le/ (LE-feasibility analysis that drove
        │                        the Plan C-1 pivot)
        ├── captures/       (UART logs from silicon validation runs)
        ├── safety/         (EPCS pre-flash dumps for revert)
        └── notes/          (point-in-time investigation notes)
```

The orchestrator does not fork or duplicate sibling sources — patches
are applied to pristine clones on demand.

## Quick start

```bash
# 1. Apply the full patch series on top of pristine sibling trees
make apply-patches
# (Post-apply tips: RoT=548920b, TPU=3206ab1.  See note below if
#  unapply/apply trips on the patch-count gotcha.)

# 2. Build chain — TPU bitstream → ROT firmware (with baked golden
#    hashes for SD artifacts) → ROT bitstream
make tpu-then-rot

# 3. Pack the SD card via stage2's mode 'W' over UART (SD stays in
#    AX301; no physical card swap)
python3 ../neorv32_rot/host/sd_pack.py --persistent --baud 115200 \
    --edits ../neorv32_rot/output/edits.bin              # single-record demo
# or for the 256-entry wide demo:
python3 ../neorv32_rot/host/gen_edits_256.py ../neorv32_rot/output/edits256.bin
python3 ../neorv32_rot/host/sd_pack.py --persistent --baud 115200 \
    --edits ../neorv32_rot/output/edits256.bin

# 4. Flash ROT to AX301 EPCS via JTAG
make flash-rot

# 5. Cold-boot the AX301 (power-cycle).  Stage2 banner prints over
#    /dev/ttyUSB0 @ 115200 baud.

# 6. Run the demos:
#    'g' → SD → TPU LUT load (raw mask)
#    'G' → SD → TPU LUT load (σ⁻¹ math applied)
#    'c' → LUT-driven compute (after g or G)
#    'Q' → read reconfig source (sanity ping)
```

### Patch-chain gotcha

`make unapply-patches` uses `HEAD~$(words $(PATCHES))` to compute the
reset target.  If you add or remove patches between an apply and an
unapply, the count drifts and unapply will reset past the true base.
Recover via `git reflog` in the sibling, find the `am: <subject of
patch 0001>` entry, reset to the commit BEFORE it.  See
`memory/feedback-orchestrator-unapply-count.md` for the full recipe.

## Configuration

Override paths via environment or `make VAR=value`:

| Variable        | Default                                                                | What it points at |
|-----------------|------------------------------------------------------------------------|-------------------|
| `ROT_REPO`      | `$(dir)/../neorv32_rot`                                                | Patched at apply-patches time |
| `TPU_REPO`      | `$(dir)/../neorv32_tpu`                                                | Patched at apply-patches time |
| `EP4CE6_REPO`   | `$(dir)/../EP4CE6`                                                     | ζ pipeline invoked read-only |
| `QUARTUS_BIN`   | `$(HOME)/intelFPGA_lite/21.1/quartus/bin`                              | Toolchain |
| `XPACK_BIN`     | `$(HOME)/xpack-riscv-none-elf-gcc-14.2.0-3/bin`                        | RISC-V toolchain |
| `OFL_LOADER`    | `$(HOME)/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader` | JTAG flasher |
| `ZETA_VERIFY`   | `1`                                                                    | Set 0 to skip ζ byte-identity gates |

## Architecture (one-screen view)

```
EP4CE10F17C8  (9,501 LE / 92 %, 1 M9K added, 16 DSP9)
┌─────────────────────────────────────────────────────────────┐
│ RoT NEORV32 SoC                                             │
│   IMEM ROM 32 KB ← stage2 + rot_golden_* + σ⁻¹ table        │
│     (immutable trust anchor; baked into bitstream)          │
│   DMEM RAM 8 KB                                             │
└──────────┬──────────────────────────────────────────────────┘
           │ XBUS
  ┌────────┼──────────────────────────────────────────┐
  │ ├ 0xF00xxxxx  wb_tpu_accel ← *** Plan C-1 ***     │
  │ │   • 4×4 int8 systolic                           │
  │ │   • 256×32-bit M9K LUT memory                   │
  │ │   • CTRL[8] LUT→PE auto-load FSM                │
  │ ├ 0xF10xxxxx  wb_sha256                           │
  │ ├ 0xF20xxxxx  wb_altasmi (EPCS)                   │
  │ ├ 0xF30xxxxx  wb_altremote (dead under C-1)       │
  │ └ 0x40000000  wb_sdram_ctrl                       │
  └───────────────────────────────────────────────────┘
       ▲                       ▲
       │ SPI                   │ JTAG (write-once per design rev)
   ┌───┴─────┐         ┌───────┴────────┐
   │ SD card │         │ EPCS (M25P16)  │
   │  edits  │ ──load→ │  RoT+TPU       │
   │   .bin  │         │  bitstream     │
   └─────────┘         └────────────────┘
```

## Phase status (vs `docs/plan.md`)

| Phase | Title | Status |
|---|---|---|
| 1 | SD-side .rbf verify (mode_t) | ✅ DONE |
| 2 | EPCS controller + ALTREMOTE_UPDATE | ✅ DONE |
| 3 | EPCS write driver + verify | ✅ DONE |
| 4 | Trigger ALTREMOTE_UPDATE | ✅ DONE |
| 5 | Build automation orchestrator | ✅ DONE |
| 6 | HW validation (AS REMOTE) | ✅ superseded by Plan C-1 (MSEL HW blocker no longer load-bearing) |
| 7 | LutCodec C port + σ⁻¹ + CRC | ✅ DONE; load-bearing under C-1 v4 |
| 8 | mode_g handler | ✅ DONE; reinterpreted as v2 (raw mask) + v4 (σ⁻¹) |
| 9 | EPCS slot rotation | moot under C-1 |
| Plan C-1 | Integrated bitstream | ✅ silicon-validated v1-v4 |

## What's not in scope (future hardening)

- **Ed25519 signature on edits.bin**: replace CRC-32 (integrity only)
  with signed integrity; public key baked into IMEM ROM `.rodata`.
- **Larger LUT memory**: 20 free M9K blocks could host ~5K entries.
- **`editable_les.bin` allowlist sidecar**: per-base allowlist of
  editable LE positions, cross-checked alongside the static `lc_init`
  whitelist.
- **Linux/FreeRTOS workload consuming TPU outputs**: orthogonal to the
  CRTM proof but would close an application-shaped story.  Project
  charter ([[project-crtm-flexibility]] memory) explicitly relaxes
  Linux as load-bearing — FreeRTOS, bare-metal, or PicoRV32 swaps are
  all fair game if LE budget pressure returns.
