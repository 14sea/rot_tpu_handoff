#!/usr/bin/env python3
"""edit_rbf.py -- apply σ⁻¹-derived XOR mask to a baseline RBF.

Loads the on-chip lutcodec/crc16 C code (compiled by build_lutcodec_so.sh
into lutcodec.so) via ctypes, runs lc_init -> lut_apply_mask ->
lc_patch_rbf_crc_frames, and writes the resulting RBF out. This is the
exact same math the NEORV32 would execute in production mode g/H, just
hosted -- a successful host-edited bitstream is necessary (but not
sufficient) for the on-chip path to work.

Pure host tool. Does NOT talk to JTAG, EPCS, or the board. Iron is left
alone; the caller is responsible for flashing the output file.

Usage:
    python3 edit_rbf.py BASELINE.rbf OUT.rbf --xyn 10,2,0 --mask 0xFFFF
"""

import argparse
import ctypes
import os
import sys


# crc16.h: CRC_NUM_CRAM_FRAMES = 1751 - 25 + 1 = 1727
# CRC_DIRTY_BITMAP_BYTES = (1727 + 7) / 8 = 216
DIRTY_BITMAP_BYTES = 216

LC_OK = 0
LC_STATUS_NAMES = {0: "LC_OK", 1: "LC_ERR_X", 2: "LC_ERR_Y", 3: "LC_ERR_N"}


class LutCodec(ctypes.Structure):
    # Must match struct lut_codec_t in lutcodec.h (base_addr; sigma[4]; bp;
    # k_odd; x; y; n). Padding handled by ctypes -- we never reach in.
    _fields_ = [
        ("base_addr", ctypes.c_uint32),
        ("sigma",     ctypes.c_uint8 * 4),
        ("bp",        ctypes.c_uint8),
        ("k_odd",     ctypes.c_uint8),
        ("x",         ctypes.c_uint16),
        ("y",         ctypes.c_uint16),
        ("n",         ctypes.c_uint16),
    ]


def load_so():
    here = os.path.dirname(os.path.abspath(__file__))
    so_path = os.path.join(here, "lutcodec.so")
    if not os.path.exists(so_path):
        sys.exit(f"lutcodec.so not found; run {here}/build_lutcodec_so.sh first")
    lib = ctypes.CDLL(so_path)

    lib.lc_init.restype = ctypes.c_int
    lib.lc_init.argtypes = [
        ctypes.POINTER(LutCodec),
        ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint16,
    ]

    lib.lut_apply_mask.restype = None
    lib.lut_apply_mask.argtypes = [
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(LutCodec),
        ctypes.c_uint16,
        ctypes.POINTER(ctypes.c_uint8),
    ]

    lib.lc_patch_rbf_crc_frames.restype = None
    lib.lc_patch_rbf_crc_frames.argtypes = [
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    return lib


def parse_xyn(s):
    parts = s.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("--xyn expects X,Y,N (e.g. 10,2,0)")
    return tuple(int(p, 0) for p in parts)


def parse_mask(s):
    m = int(s, 0)
    if not (0 <= m <= 0xFFFF):
        raise argparse.ArgumentTypeError("--mask must be 0..0xFFFF")
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("baseline", help="input mask=0x0000 .rbf")
    ap.add_argument("out", help="output .rbf path")
    ap.add_argument("--xyn", type=parse_xyn, required=True,
                    metavar="X,Y,N", help="LE coords, e.g. 10,2,0")
    ap.add_argument("--mask", type=parse_mask, required=True,
                    help="16-bit target LUT mask")
    args = ap.parse_args(argv)

    lib = load_so()

    with open(args.baseline, "rb") as fh:
        rbf = bytearray(fh.read())
    print(f"baseline: {args.baseline} ({len(rbf)} bytes)")

    rbf_buf = (ctypes.c_uint8 * len(rbf)).from_buffer(rbf)

    lc = LutCodec()
    x, y, n = args.xyn
    st = lib.lc_init(ctypes.byref(lc), x, y, n)
    if st != LC_OK:
        sys.exit(f"lc_init failed: {LC_STATUS_NAMES.get(st, st)} for X={x} Y={y} N={n}")
    print(f"lc_init OK: base_addr=0x{lc.base_addr:08x} bp={lc.bp} "
          f"sigma={list(lc.sigma)} k_odd={lc.k_odd}")

    dirty = (ctypes.c_uint8 * DIRTY_BITMAP_BYTES)()
    lib.lut_apply_mask(rbf_buf, ctypes.byref(lc), args.mask, dirty)
    # Count flipped frames for sanity (popcount).
    dirty_frames = sum(bin(b).count("1") for b in bytes(dirty))
    print(f"lut_apply_mask: mask=0x{args.mask:04x} "
          f"popcount={bin(args.mask).count('1')} dirty_frames={dirty_frames}")

    lib.lc_patch_rbf_crc_frames(rbf_buf, dirty)
    print(f"lc_patch_rbf_crc_frames: repaired {dirty_frames} frames")

    with open(args.out, "wb") as fh:
        fh.write(rbf)
    print(f"wrote: {args.out} ({len(rbf)} bytes)")


if __name__ == "__main__":
    main()
