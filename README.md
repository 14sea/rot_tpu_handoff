# rot_tpu_handoff

Cross-repo orchestration for the **NEORV32 ROT → SD-staged TPU bitstream
→ EPCS self-reconfiguration** flow on AX301 (Cyclone IV E EP4CE6F17C8).

This repo holds **only orchestration + patches** — none of the three
sibling projects it depends on are forked or duplicated:

```
~/                          (parent of all repos)
├── neorv32_rot/            (pristine; sibling, untouched)
├── neorv32_tpu/            (pristine; sibling, untouched)
├── EP4CE6/                 (pristine; sibling, untouched — RE work
│                            consulted only at build time for ζ gates)
└── rot_tpu_handoff/        (this repo)
    ├── README.md
    ├── Makefile            (cross-repo build orchestrator)
    ├── patches/
    │   └── neorv32_rot/    (git-am-style patches that extend ROT
    │       │                with TPU-bitstream awareness)
    │       ├── 0001-Phase-1-SD-side-TPU-bitstream-verify-mode-t-no-EPCS-.patch
    │       └── 0002-Phase-5-top-level-Makefile-tpu-then-rot-build-chain.patch
    └── docs/
        └── plan.md         (full 6-phase implementation plan)
```

## Why this layout

The dynamic-switching feature **does not need bitstream RE** (vendor IP
solves it; see `docs/plan.md` Open-toolchain section). To keep the
EP4CE6 RE directory clean and to avoid burdening the upstream
`neorv32_rot` project with cross-cutting orchestration code, all of it
lives here. The patches are versioned in this repo and can be applied
on demand to a pristine ROT clone.

## Quick start

```bash
# 1. Apply the firmware + sd-tooling extensions on top of pristine ROT
make apply-patches

# 2. (Optional) verify they reverse cleanly
make unapply-patches && make apply-patches

# 3. Full build chain — TPU bitstream → tpu.rbf → ROT firmware (with
#    baked TPU sha256) → ROT bitstream
make tpu-then-rot

# 4. Pack SD card with all 4 payloads (Image, DTB, initrd, TPU.rbf)
make sd-pack DEVICE=/dev/sdX

# 5. Flash ROT to AX301 EPCS via JTAG
make flash-rot
```

Each `make` target is a thin wrapper around the canonical commands
documented in `docs/plan.md`; nothing is hidden.

## Configuration

Override paths via environment or `make VAR=value`:

| Variable        | Default                                          | What it points at |
|-----------------|--------------------------------------------------|-------------------|
| `ROT_REPO`      | `$(dir)/../neorv32_rot`                          | Patched at apply-patches time |
| `TPU_REPO`      | `$(dir)/../neorv32_tpu`                          | Built read-only |
| `EP4CE6_REPO`   | `$(dir)/../EP4CE6`                               | ζ pipeline invoked read-only |
| `QUARTUS_BIN`   | `$(HOME)/intelFPGA_lite/21.1/quartus/bin`        | Toolchain |
| `XPACK_BIN`     | `$(HOME)/xpack-riscv-none-elf-gcc-14.2.0-3/bin`  | Toolchain |
| `OFL_LOADER`    | `$(HOME)/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader` | JTAG flasher |
| `DEVICE`        | (unset; required for `sd-pack`)                  | SD card block device |
| `ZETA_VERIFY`   | `1`                                              | Set 0 to skip ζ byte-identity gates |

## Why ROT itself isn't modified upstream

The two patches touch:

- `host/sd_layout.py` v2 → v3 (TPU.rbf slot at LBA 8009, 4th sha256 slot)
- `host/sd_pack.py` (`--tpu-rbf` arg, 4th segment)
- `host/gen_golden.py` (4th SECTIONS entry, env-overridable path)
- `sw/stage2_loader/main.c` (extends `sd_boot_hdr` v3, adds `mode_tpu_verify`,
  dispatches `'t'`)
- `Makefile` (top-level `tpu-then-rot` orchestrator)

The first four are arguably ROT-side improvements. The last is pure
cross-cutting. Bundling them as a patch series keeps the upstream
ROT clone canonical (`5a42a81` = "Option A: bitstream-anchored CRTM")
and lets this overlay come and go without polluting it.

If a future ROT release wants to upstream parts of this work, the
patches are git-format-patch shaped and `git am`-applicable directly.

## Plan reference

`docs/plan.md` is the full 6-phase implementation plan
(originally `~/.claude/plans/temporal-booping-raven.md`). Phases 1 + 5
are landed in the patches; Phases 2-4 + 6 are pending and require
Quartus IP integration + hardware bench work.
