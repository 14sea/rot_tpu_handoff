# Linux + TPU demo — "dynamic load + trust anchor → useful compute"

Orthogonal demo (candidate **(d)**): boot nommu Linux on the NEORV32 soft-core
from an in-bitstream trust anchor, then run a **quantized dense-layer
classifier** on the integrated 4×4 int8 TPU from Linux userspace, with the
mutable workload (weights) loaded + trust-verified at runtime. Realizes
[[project-crtm-flexibility]] (OS substitutable; load-bearing = dynamic-load +
trust-anchor → useful compute).

Combines two proven siblings, both on the **same board** (AX301, Cyclone IV
**EP4CE10F17C8**, 32 MB SDRAM, 50 MHz):
* `see_neorv32_run_linux` — nommu Linux 6.6.83 on NEORV32 RV32IMAC (boots to
  `nommu#` in ~20 s; D-cache off).
* `neorv32_tpu` / the C-1 in-repo TPU RTL — 4×4 int8 systolic MAC + register
  file (the same peripheral the RoT drives via mode c).

Per [[feedback-copy-into-handoff-repo]], everything operates from a scratch
merge inside this repo; the siblings are not mutated (`neorv32` is a read-only
symlink to the patched Linux submodule).

## Phases

> ## ✅ SILICON-VALIDATED end-to-end (2026-05-31)
> Booted nommu Linux on the combined Linux+TPU bitstream (AX301/EP4CE10), `/init`
> auto-ran the trust-gated classifier on the fabric TPU via direct U-mode MMIO:
> - **good model** → `trust OK` → `RES=[200,300,−100,100]` → **class 1 (score 300)** — byte-identical to `host_reference.py`.
> - **tampered model** → `TRUST FAIL: model hash != anchor — refusing to load fabric`.
>
> Logs: `iron_run1.log` (good), `iron_run2_tampered.log` (refused). The full
> chain — *dynamic-load + trust-anchor → useful compute under Linux* — runs on iron.

| | phase | status |
|-|-------|--------|
| P1 | Integrate TPU into the Linux SoC + build combined bitstream | ✅ done |
| P2 | Reach the TPU from Linux userspace | ✅ **silicon-validated** |
| P3 | Quantized dense-layer classifier (load + trust-verify weights → infer) | ✅ **silicon-validated** |
| P4 | **EPCS-persist** — cold-boot → Linux → classifier, no host | ✅ **silicon-validated** → **`EPCS_PERSIST_RUNBOOK.md`** |

**P4 (EPCS-persist) — ✅ silicon-validated 2026-05-31:** the demo is now
autonomous on power-on. Persist build sets `BOOT_MODE_SELECT=2` so stage2 is
baked into IMEM-ROM (trust anchor in the immutable bitstream) and auto-boots
Linux from the SD `NEOLNX` blob; bitstream → EPCS page 0,
kernel/dtb/classifier-initramfs → SD card. `neorv32_demo_persist.rbf` (md5
`d85d5214`, 5,418 LE, IMEM-ROM, 0 errors) burned to EPCS; **a bare power-cycle
(no host, no UART upload, no openFPGALoader) boots Linux and auto-runs the
classifier → class 1 (score 300)** — `iron_persist_coldboot.log` /
`iron_persist_sram_validate.log`. Full iron procedure in
**`EPCS_PERSIST_RUNBOOK.md`**. Revert to RoT C-1 v4 via the `docs/safety/` backup.

**P2/P3 approach — corrected from the original "mmap /dev/mem" plan.** The real
rootfs is a 2.9 KB nolibc static `init` (no busybox, no `/dev/mem`,
`CONFIG_DEVMEM` off, nommu, no loadable modules). But it's nommu with **PMP off**
(`PMP_NUM_REGIONS=0`), so U-mode userspace can poke MMIO directly — the stock
init already does this for the UART (`diag_putc`). So P2+P3 collapse into: extend
that init to drive the TPU at `0xF0000000` directly. **No kernel rebuild, no DT
node, no driver, no busybox** — kernel + DTB + stage2 are reused unchanged.

## P1 result (2026-05-31) — combined bitstream builds & fits

`scratch_merge/` = Linux SoC (`ax301_top.vhd`, `wb_sdram_ctrl.v`, `sdram_ctrl.v`,
`neorv32`→symlink) + TPU RTL (`rtl/tpu/{wb_tpu_accel,tpu_accel,systolic_array_4x4,pe}.v`).

**Integration**: the Linux SoC wired XBUS straight to `wb_sdram_ctrl` (SDRAM
implicitly ack'd the whole XBUS range). Added an XBUS decode/mux in
`ax301_top.vhd`: `0xF0000000–0xF00FFFFF` → `wb_tpu_accel`, every other XBUS
cycle → SDRAM (unchanged). TPU register map identical to the RoT's, so its
semantics transfer directly:

```
0x000 CTRL  [0]=start [4]=clear [8]=load_from_lut   0x010 X_IN  {x3..x0} int8
0x004 STATUS[0]=done [1]=lut_busy                   0x014 W_DATA4 {w3..w0} int8 (bulk row)
0x008 W_ADDR[1:0]=col [3:2]=row                     0x020-0x02C RES0-3 int32  (= Σ_col W[row][col]·X[col])
0x00C W_DATA[7:0]=weight byte                        => a 4-input → 4-output quantized dense layer
```

**Build** (Quartus Lite 21.1, quartus_map → fit → asm → sta):

| metric | Linux only (baseline) | **Linux + TPU** |
|--------|----------------------|-----------------|
| Logic elements | 4,405 / 10,320 (43%) | **5,459 / 10,320 (53%)** |
| DSP9 | 0 | 16 / 46 (35%) |
| Memory bits | — | 175,104 / 423,936 (41%) |
| Synthesis / Fit / Asm | — | **0 errors** |

47% LE headroom. No new pins (TPU is internal on XBUS).

**TPU reachability (static, confirmed)**: NEORV32's bus de-mux (`neorv32_top.vhd`
ports A=IMEM, B=DMEM, C=IO @ `mem_io_base_c=0xFFE00000`, **X="the void"=XBUS**)
routes every non-internal address to XBUS. `0xF0000000` < `0xFFE00000` and isn't
IMEM/DMEM → XBUS → our decode → TPU. (SDRAM @ `0x40000000` reaches XBUS the same way.)

**Timing (honest caveat)**: worst-case setup slack **−0.959 ns** at 50 MHz — but
the worst path is `S_DB[*] → sdram_ctrl|data_lo[*]` (S_CLK→CLOCK), the
**pre-existing SDRAM read-capture path**, dominated by −2.947 ns clock skew.
The baseline Linux build has the **same path negative (−0.715 ns)** and boots
Linux reliably at 50 MHz, so this is functionally tolerated on this board (slow-
corner pessimism + the SDC lacks `derive_clock_uncertainty`). The TPU adds
~0.24 ns placement pressure but introduces **no new critical path** (TPU paths
are not among the worst). → low risk; verify by actual boot in P2. SDC kept
identical to the known-good baseline on purpose.

Bitstream: `scratch_merge/quartus/neorv32_demo.sof`.

## P2/P3 host-prep result (2026-05-31)

Built + verified host-side (no iron):
- `initramfs/init.c` — extends the stock init with a `tpu` classifier: reads
  `/tpu_model.bin`, FNV-1a-32 **trust-gates** the 16 model bytes vs a baked-in
  anchor, then drives the TPU (load W via `W_ADDR`/`W_DATA`, set `X_IN`, start,
  poll `STATUS`, read `RES0-3`, argmax). Auto-runs at boot + `tpu` command.
  Cross-compiles (riscv32-buildroot-linux-gnu, static-PIE) to **4,780 B**.
- `initramfs/neo_initramfs.cpio.gz` — **2,241 B** (`/init` + `/tpu_model.bin` +
  `/dev/console`); fits the initramfs budget trivially.
- `host_reference.py` — golden model/query + expected `RES=[200,300,-100,100]`,
  **class 1**, model hash `0x9CA88565`; emits `tpu_model.bin` (+ tampered copy).
- `scratch_merge/quartus/neorv32_demo.rbf` — combined bitstream (from P1).

Remaining = the iron session (needs the board, your cold-boot): see
**`IRON_RUNBOOK.md`**. Reuses kernel/DTB/stage2 unchanged; only `--rbf`
(combined) and `--initrd` (this one) differ. Expected: auto-runs → `trust OK` →
`predicted class = 1 (score 300)`; tampered model → `TRUST FAIL`.
