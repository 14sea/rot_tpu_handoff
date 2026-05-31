# Linux + TPU demo — EPCS-persist iron runbook (cold-boot autonomy)

Goal: **power-on → Linux boots → classifier auto-runs**, with no host, no UART
upload, no `boot_linux.py`. Built on the silicon-validated SRAM demo
(`README.md` / `IRON_RUNBOOK.md`); this runbook makes it persistent.

## What changed to get cold-boot autonomy

Three UART dependencies had to be broken, not two:

| element | SRAM demo (today) | EPCS-persist |
|---|---|---|
| FPGA bitstream | SRAM-loaded each boot | **EPCS page 0** (AS config at power-on) |
| **stage2 loader** | host UART-uploads via NEORV32 bootloader | **baked into IMEM-ROM** (`BOOT_MODE_SELECT=2`) |
| kernel + dtb + initramfs | UART xmodem (`boot_linux.py`) | **SD card** NEOLNX blob (`sd_pack.py`) |

The persist bitstream sets `BOOT_MODE_SELECT => 2` in `ax301_top.vhd`: the
NEORV32 bootloader is gone; the CPU resets straight into **stage2 baked into
IMEM-ROM** (`rtl/neorv32_imem_image.vhd`, generated from `sw/stage2_loader/`).
stage2's dispatcher was patched so a **no-host 3 s timeout defaults to
`mode_sd_boot()`** (was U-Boot) — it reads kernel/dtb/initramfs from the SD
`NEOLNX` blob and jumps. The SD-boot (`'b'`) path itself is the already-validated
`see_neorv32_run_linux` daily flow; we only changed the *default*.

CRTM-aligned: the trust anchor (stage2) now lives inside the immutable
bitstream; the SD card holds only mutable workload data (kernel/rootfs/model).

## Build artifacts (host-side, already produced — no iron)

In `scratch_merge/`:
- `quartus/neorv32_demo_persist.rbf` — combined Linux+TPU, **BOOT_MODE_SELECT=2**,
  368011 B, md5 `d85d52146835907455a9f761d8a1c13c`.
  - Fit: 5,418 / 10,320 LE (53%), 16 DSP, 0 errors. IMEM-ROM, no BOOTROM, SPI/SD
    present, TPU @0xF0000000 present (172 refs).
  - Worst setup −0.760 ns @50 MHz on `S_DB[15]→wb_sdram_ctrl|sdram_ctrl|data_lo[15]`
    (S_CLK→CLOCK, −2.5 ns skew) = the documented pre-existing SDRAM read-capture
    path; baseline boots Linux reliably in this regime. No TPU critical path.
- `sw/stage2_loader/` — patched stage2 (default-timeout → SD-boot). `elf.bin`
  7904 B = 1976 words, fits the 2048-word (8 KB) IMEM-ROM with headroom.
- `rtl/neorv32_imem_image.vhd` — IMEM-ROM init = the patched stage2
  (md5 `dd5deba621e5d1dad3094c9515167b09`). Generated locally (sibling `neorv32`
  never mutated; `image_gen` compiled into the handoff dir).

To regenerate stage2 + image: `cd scratch_merge/sw/stage2_loader && gcc -O2 -o
image_gen /home/test/see_neorv32_run_linux/neorv32/sw/image_gen/image_gen.c &&
make image RISCV_PREFIX=/home/test/xpack-riscv-none-elf-gcc-14.2.0-3/bin/riscv-none-elf-
NEORV32_HOME=/home/test/see_neorv32_run_linux/neorv32 IMAGE_GEN=$PWD/image_gen`,
then `cp neorv32_imem_image.vhd ../rtl/` and rebuild the bitstream.

## Tools / constants
- openFPGALoader: `/home/test/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader`
- spiOverJtag bridge (EP4CE10F17C8): auto-detected (`spiOverJtag_ep4ce1017.rbf.gz`).
- EPCS chip: ST M25P16, 2 MB (32 × 64 KB sectors). Console baud 115200.
- **Current EPCS = RoT C-1 v4 CRTM deliverable** (page-0 .rbf md5 `fd884770`,
  modes g/G/c/H). Existing full backup: `docs/safety/epcs_backup_20260521_pre_modeH.bin`
  (md5 `b81dc43b`). This runbook **overwrites page 0** — restorable from backup.

---

## Iron sequence

`OFL=/home/test/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader`
Cold-boot / power-cycle is **your** action. Listener-first on every boot.

### Step 0 — fresh EPCS backup (before anything)
```
$OFL -c usb-blaster --dump-flash --file-size 2097152 \
    docs/safety/epcs_backup_$(date +%Y%m%d_%H%M)_pre_persist.bin
md5sum docs/safety/epcs_backup_*_pre_persist.bin   # expect b81dc43b (== v4 baseline)
```
If the md5 ≠ `b81dc43b`, STOP and reconcile — EPCS isn't the expected v4 baseline.

### Step 1 — write the SD card (NEOLNX blob with the classifier initramfs)
SD packing goes over UART via stage2 mode `W`, which needs a **UART-bootloader**
bitstream (the persist build has no bootloader). Use the stock
`see_neorv32_run_linux` dev flow; SD content is bitstream-independent.
```
cd /home/test/see_neorv32_run_linux
RT=/home/test/rot_tpu_handoff/docs/analysis/linux_tpu
python3 host/sd_pack.py --port /dev/ttyUSB0 \
    --initrd $RT/initramfs/neo_initramfs.cpio.gz
```
(kernel `output/Image`, dtb `output/neorv32_ax301.dtb` are the stock defaults.)
This SRAM-loads the dev bitstream, uploads stage2, and writes:
LBA0 header / LBA1 Image / LBA4001 DTB / LBA4009 initrd(classifier). ~99 s.
The classifier initrd is only 2241 B — trivially within its 2 MB slot.

> Prereq: build the classifier initramfs if absent —
> `cd $RT/initramfs && make` (runs `host_reference.py` for `tpu_model.bin`).

### Step 2 — PRE-BURN end-to-end validation (SRAM, non-destructive)
Because stage2 is in IMEM-ROM, **SRAM-loading the persist .rbf exercises the
exact cold-boot path** (config → IMEM-ROM stage2 → auto SD-boot → classifier)
**without touching EPCS**. This is the discriminating test before the burn.

Listener-first, then SRAM-load (note: **no `-f`** → SRAM only, EPCS untouched):
```
cat /dev/ttyUSB0            # terminal A, listener-first
$OFL -c usb-blaster -m \
    /home/test/rot_tpu_handoff/docs/analysis/linux_tpu/scratch_merge/quartus/neorv32_demo_persist.rbf
```
Expect, with **no host interaction**:
```
[stage2] ready - RV32IMAC NEORV32 loader
[stage2] ... b/timeout=bootSD u=U-Boot
[stage2] Mode: SD blob boot
[sd] image_sz=... dtb_sz=... initrd_sz=...
... (kernel boot) ...
--- Linux+TPU dense-layer classifier (auto) ---
[tpu] model hash = 0x9ca88565  anchor = 0x9ca88565
[tpu] trust OK; ...  RES[0..3]=200,300,-100,100
[tpu] >>> predicted class = 1  (score 300)
nommu#
```
If this PASSES, the persist bitstream is end-to-end correct → proceed to burn.
If stage2 doesn't auto-SD-boot (e.g. sits idle / U-Boot), do NOT burn; debug
first (likely SD-blob/sd.c or the timeout default). Save the log to
`docs/analysis/linux_tpu/iron_persist_sram_validate.log`.

### Step 3 — burn persist bitstream to EPCS page 0
```
$OFL -c usb-blaster -f \
    /home/test/rot_tpu_handoff/docs/analysis/linux_tpu/scratch_merge/quartus/neorv32_demo_persist.rbf
```
`-f` writes bit-reversed bytes to EPCS (the AS-config convention; cold-boot
decodes correctly — proven on this board for the RoT builds).

Static-verify the readback (dump page-0 region, bit-reverse, compare to the .rbf):
```
$OFL -c usb-blaster --dump-flash --file-size 2097152 \
    docs/safety/epcs_backup_$(date +%Y%m%d_%H%M)_post_persist.bin
# page-0 region [0 .. 368011) bit-reversed must equal neorv32_demo_persist.rbf
python3 - <<'PY'
br=bytes(int(f'{b:08b}'[::-1],2) for b in range(256))
rbf=open('docs/analysis/linux_tpu/scratch_merge/quartus/neorv32_demo_persist.rbf','rb').read()
import glob; dump=open(sorted(glob.glob('docs/safety/*_post_persist.bin'))[-1],'rb').read()
got=bytes(br[b] for b in dump[:len(rbf)])
print('page-0 byte-identical:', got==rbf, 'len', len(rbf))
PY
```
Expect `page-0 byte-identical: True`.

### Step 4 — cold-boot (YOUR action)
Power-cycle the board (full power-off/on, not just KEY2). Listener-first:
```
cat /dev/ttyUSB0            # BEFORE powering on
```
Expect the **same autonomous output as Step 2**, now sourced entirely from
EPCS + SD — no host, no openFPGALoader, no UART upload. Save to
`docs/analysis/linux_tpu/iron_persist_coldboot.log`.

Tamper check (optional): re-pack SD with `tpu_model_tampered.bin` (Step 1 with
`--initrd` pointing at a tampered repack) → cold-boot → expect `TRUST FAIL`.

## Recovery
- Revert EPCS to the RoT C-1 v4 deliverable:
  `$OFL -c usb-blaster -f --file-type raw docs/safety/epcs_backup_20260521_pre_modeH.bin`
  (full 2 MB raw restore; same method used at the end of the mode-H session).
- The persist bitstream has **no UART bootloader** — if stage2/SD is wrong, you
  cannot UART-upload a fix; reprogram via openFPGALoader (SRAM `-m` to recover a
  shell, or `-f` to reflash EPCS). This is why Step 2 (SRAM validation) precedes
  the burn.

## Watch-items (first silicon of this configuration)
1. **First `BOOT_MODE_SELECT=2` boot on this board** — IMEM-ROM reset vector +
   stage2-from-ROM. Mitigated by Step 2 SRAM validation (identical path).
2. **First EPCS cold-boot of the Linux+TPU SoC** — the SRAM demo never sat in
   EPCS. Standard Quartus AS .rbf for this device; RoT builds cold-boot from EPCS
   fine. Step 3 readback verify + Step 4 listener confirm.
3. **SD read at cold-boot** — `sd.c` SPI init must succeed from a cold SPI bus
   (no prior host SPI traffic). `sd_smoke`/`sd_pack` already exercise it; if
   `[sd] init FAIL`, retry / reseat card.
4. **IMEM headroom** — stage2 is 7904 / 8192 B. Any stage2 growth must stay
   < 8 KB or bump `IMEM_SIZE` (costs M9K, not LE; 47% LE headroom regardless).
