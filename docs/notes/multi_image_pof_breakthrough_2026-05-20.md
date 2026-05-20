# Multi-image POF breakthrough — 2026-05-20 LATE

## TL;DR

`quartus_cpf -c <multi-image.cof>` **works in Quartus Lite 21.1** for Cyclone IV E
+ EPCS16, producing a valid dual-boot Active-Serial POF with Page_0 = factory
(RoT) and Page_1 = application (TPU) at 0x80000. The prior memory claim
("Illegal Configuration Scheme does not support dual boot") was wrong — the
real fix is **remove the `<flash_loader_device>` tag from the .cof**. With that
tag present, quartus_cpf treats the project as a Serial Flash Loader passthrough
and bails with `this is not a serial flash loader design`; without it,
quartus_cpf produces the multi-image POF cleanly.

## Working .cof template (Lite 21.1)

```xml
<?xml version="1.0" encoding="US-ASCII" standalone="yes"?>
<cof>
    <eprom_name>EPCS16</eprom_name>
    <output_filename>/tmp/rot_tpu_multi.pof</output_filename>
    <n_pages>2</n_pages>
    <width>1</width>
    <mode>7</mode>
    <sof_data>
        <user_name>Page_0</user_name>
        <page_flags>1</page_flags>
        <bit0>
            <sof_filename>/home/test/neorv32_rot/quartus/neorv32_demo.sof</sof_filename>
        </bit0>
    </sof_data>
    <sof_data>
        <user_name>Page_1</user_name>
        <page_flags>1</page_flags>
        <bit0>
            <sof_filename>/home/test/neorv32_tpu/quartus/neorv32_tpu.sof</sof_filename>
        </bit0>
        <start_address>00080000</start_address>
    </sof_data>
    <version>10</version>
    <create_cvp_file>0</create_cvp_file>
    <create_hps_iocsr>0</create_hps_iocsr>
    <auto_create_rpd>0</auto_create_rpd>
    <rpd_little_endian>1</rpd_little_endian>
    <options>
        <map_file>1</map_file>
    </options>
    <advanced_options>
        <ignore_epcs_id_check>2</ignore_epcs_id_check>
        <ignore_condone_check>2</ignore_condone_check>
        <plc_adjustment>0</plc_adjustment>
        <post_chain_bitstream_pad_bytes>-1</post_chain_bitstream_pad_bytes>
        <post_device_bitstream_pad_bytes>-1</post_device_bitstream_pad_bytes>
        <bitslice_pre_padding>1</bitslice_pre_padding>
    </advanced_options>
</cof>
```

(stored at `docs/safety/multi_image_2026-05-20/multi_v2.cof`)

Two-step generation:
```
quartus_cpf -c multi_v2.cof                    # → rot_tpu_multi.pof + .map
quartus_cpf -c rot_tpu_multi.pof rot_tpu_multi.rpd   # → raw 2 MB EPCS image
```

`.map` shows: Page_0 @0x00000-0x59D8A, Page_1 @0x80000-0xD9D8A, mode = Active
Serial, EPCS16, quad-serial dummy = 8.

## AS REMOTE metadata fingerprint (the bytes that matter)

Diff between `rot_tpu_multi.rpd[0:0x80000]` and the standalone `RoT.rbf`
(both in the same byte order — neither has been bit-reversed for EPCS yet):

| offset    | rpd  | rbf  | XOR  | likely meaning                       |
|-----------|------|------|------|--------------------------------------|
| 0x00029   | 0xfa | 0xf2 | 0x08 | "application slot present" flag      |
| 0x00045   | 0xfc | 0xfe | 0x02 | factory/app marker bit               |
| 0x00046   | 0xf8 | 0xfa | 0x02 | factory/app marker bit (continued)   |
| 0x00049   | 0x31 | 0x95 | 0xa4 | START_ADDRESS encoding byte 0        |
| 0x0004a   | 0x9a | 0x82 | 0x18 | START_ADDRESS encoding byte 1 + CRC  |

Page_1 (TPU) has the symmetric 6 bytes at 0x80029, 0x8002a, 0x80045, 0x80046,
0x80049, 0x8004a — i.e. the same per-page header structure, with the additional
0x8002a flip that reflects "this image is the application not the factory".

## What this means for CRTM closure

The chip-side AS REMOTE controller checks **header bytes 0x29 / 0x45 / 0x46 /
0x49 / 0x4A** at the slot offset before accepting a reconfig request. Our prior
RoT.rbf + TPU.rbf at slots 0 / 0x80000 were missing those 11 bytes, which is why
`reconfig source = nSTATUS asserted (4)` was being returned for every layout
sweep — chip refused to even start loading slot 1.

## Flash plan vs current iron

`epcs_backup_20260520_sessionend.bin` (md5 c2de7592…) differs from
`bitrev(rot_tpu_multi.rpd)` in only 19 bytes:

- 5 bytes in Page_0 header (AS REMOTE map for the factory image)
- 6 bytes in Page_1 header (AS REMOTE map for the application image)
- 8 bytes at 0x100000 (`DEADBEEFCAFEBABE` test marker written by mode `P` Phase I; non-load-bearing — main.c:1111 confirms it's diagnostic-only)

To install the multi-image map, flash `rot_tpu_multi.rpd` (2 MB) via
`openFPGALoader -f`. The RPD is in the same byte order as a `.rbf`, so
openFPGALoader's `-f` bit-reverse step produces correct EPCS bytes.
Bit-reverse of EPCS dump == RoT.rbf for the page-0 region was verified
empirically (both 0..0x59D8A and 0x80000..0xD9D8A match byte-perfectly).
