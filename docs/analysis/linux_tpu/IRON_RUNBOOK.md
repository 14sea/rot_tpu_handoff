# Linux + TPU demo — iron runbook (P2 + P3)

Everything host-side is built. This is the on-board session (needs the AX301 +
USB-Blaster + UART). Cold-boot / programming is your action
([[feedback-flash-vs-reboot-authority]]). FPGA programming here is **SRAM-load
(volatile)** — `openFPGALoader -c usb-blaster <rbf>`, no `-f` — so it does **not**
touch EPCS; a power-cycle reverts to whatever is in flash. Non-destructive.

## Prerequisites — regenerate build outputs if absent
The two flashable artifacts are git-ignored build outputs (not committed); they
exist after this session but a fresh checkout/`git clean` must regenerate them:
- **initramfs** (`initramfs/neo_initramfs.cpio.gz`): `cd initramfs && make`
  (needs `tpu_model.bin` — `cd .. && python3 host_reference.py` if missing).
- **combined .rbf** (`scratch_merge/quartus/neorv32_demo.rbf`): from the P1
  build — `quartus_cpf -c neorv32_demo.sof neorv32_demo.rbf` in
  `scratch_merge/quartus/` (or rebuild: `quartus_map && quartus_fit && quartus_asm`).
- The tampered-demo initramfs (`neo_initramfs_tampered.cpio.gz`) is built per Step 3.

## What changed vs a normal see_neorv32_run_linux boot
Only two inputs differ; **kernel, DTB, and stage2 are reused unchanged** (the TPU
is reached by direct U-mode MMIO, so the kernel needs no TPU node/driver):

| input | normal | this demo |
|-------|--------|-----------|
| `--rbf` | Linux-only | **combined Linux+TPU** `docs/analysis/linux_tpu/scratch_merge/quartus/neorv32_demo.rbf` (368011 B) |
| `--initrd` | stock | **`docs/analysis/linux_tpu/initramfs/neo_initramfs.cpio.gz`** (2241 B: init + /tpu_model.bin) |
| kernel / dtb / stage2 | defaults | **defaults (unchanged)** |

## Step 1 — boot
From the `see_neorv32_run_linux` checkout (it has the board tooling + kernel/dtb/stage2):

```
RT=/home/test/rot_tpu_handoff/docs/analysis/linux_tpu
python3 host/boot_linux.py \
    --rbf    $RT/scratch_merge/quartus/neorv32_demo.rbf \
    --initrd $RT/initramfs/neo_initramfs.cpio.gz
```
This: programs the combined bitstream (SRAM) → talks to the NEORV32 UART
bootloader (19200) → uploads stage2 → stage2 xmodem-loads kernel @0x40000000,
DTB @0x41F00000, initramfs @0x41F80000 → jumps to the kernel. ~20 s to shell.

## Step 2 — expected success output
The init auto-runs the classifier at boot. Expect (golden values from
`host_reference.py`):
```
========================================
 NEORV32 nommu Linux + TPU — mini shell
========================================
...
--- Linux+TPU dense-layer classifier (auto) ---
[tpu] reading workload /tpu_model.bin ...
[tpu] model hash = 0x9ca88565  anchor = 0x9ca88565
[tpu] trust OK; programming TPU weights (4x4 int8).
  RES[0] = 200
  RES[1] = 300
  RES[2] = -100
  RES[3] = 100
[tpu] >>> predicted class = 1  (score 300)  [expect class 1, score 300]
-----------------------------------------------
nommu#
```
`tpu` at the `nommu#` prompt re-runs it. RES[row] = Σ_col W[row][col]·X[col]
computed on the fabric TPU — this is Linux userspace consuming TPU output.

## Step 3 — trust-anchor demo (tamper → refuse)
Repack the initramfs with the tampered model in place of the good one, reboot:
```
cd $RT/initramfs
cp tpu_model_tampered.bin tpu_model.bin && make           # repack with bad model
# boot as Step 1; then restore: (cd .. && python3 host_reference.py) && make
```
Expect the gate to refuse (no fabric write):
```
[tpu] model hash = 0xfcddd2e4  anchor = 0x9ca88565
[tpu] TRUST FAIL: model hash != anchor — refusing to load fabric.
```

## Risks / watch-items (first silicon of this combination)
1. **First boot of the combined bitstream.** Timing worst-path is the
   pre-existing SDRAM capture path, not the TPU (P1 README), and the baseline
   boots Linux fine — but this is the first actual boot with the TPU present.
2. **Direct W_ADDR/W_DATA weight-load path:** silicon mode-c validated the
   *LUT-auto-load* path (CTRL[8]); this demo uses the direct per-PE load. It is
   straightforward RTL (W_DATA → load_weight pulse) but is being silicon-
   exercised here for the first time. If RES is wrong but non-zero, suspect
   weight-load spacing; fall back to the LUT-load path (write weights into TPU
   LUT @0x040+i*4 bits[23:16], CTRL[8], poll STATUS[1], then compute).
3. **Direct U-mode MMIO to 0xF0000000** relies on PMP being off (verified:
   PMP_NUM_REGIONS=0) — the init's existing `diag_putc` UART poke already proves
   U-mode MMIO works on this system.
4. **D-cache is off** → TPU register reads/writes are coherent (no flush needed).

## If direct MMIO unexpectedly faults
Fallback is the built-in platform-driver path (sysfs), which does the register
I/O in kernel (M-mode) space — more code + a kernel rebuild. Not expected to be
needed given (3).
