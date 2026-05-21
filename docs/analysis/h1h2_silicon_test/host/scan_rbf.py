#!/usr/bin/env python3
"""scan_rbf.py -- reverse-engineer 4-input LUT contents at every pin-friendly
LE position in a Cyclone IV E .rbf.

Walks (X, Y, N) over LUTCODEC_NUM_X × LUTCODEC_NUM_Y × LUTCODEC_NUM_N. For
each candidate it calls lc_init via ctypes, reads the 16 LUT cells through
the same address formula lut_apply_mask uses, and reconstructs the LE's
current truth-table mask. Prints any LE whose mask matches a search target
(default 0xA5A5 -- the h1h2_top sentinel signature) and optionally probes a
specific (x, y, n) so we can answer "did the location_assignment land?"

Used as a precondition for edit_rbf.py: edit_rbf.py needs (X, Y, N) to know
WHERE to apply σ⁻¹(target_mask ^ baseline_mask). When Quartus Lite silently
drops the location lock, scan_rbf.py is how we recover that coordinate.
"""

import argparse
import ctypes
import os
import sys


PIN_X = [10, 16, 22, 28]
LAB_Y = [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 21]
EVEN_N = list(range(0, 32, 2))
PAIR_SPACING = 210

LC_OK = 0
LC_STATUS_NAMES = {0: "LC_OK", 1: "LC_ERR_X", 2: "LC_ERR_Y", 3: "LC_ERR_N"}


class LutCodec(ctypes.Structure):
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
    return lib


def read_mask_at(rbf, lc):
    """Reconstruct the 16-bit LUT mask currently stored at (lc.x, lc.y, lc.n)."""
    mask = 0
    sigma = list(lc.sigma)
    for b in range(16):
        f0 = (b >> sigma[0]) & 1
        f1 = (b >> sigma[1]) & 1
        f2 = (b >> sigma[2]) & 1
        f3 = (b >> sigma[3]) & 1
        pair = ((1 - f0) << 2) | ((1 - f1) << 1) | (1 - f2)
        da = f3 ^ (1 - f2)
        delta = (1 - da) if lc.k_odd else da
        addr = lc.base_addr + pair * PAIR_SPACING + delta
        if addr < len(rbf):
            bit = (rbf[addr] >> lc.bp) & 1
            if bit:
                mask |= (1 << b)
    return mask


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("rbf", help="input .rbf to scan")
    ap.add_argument("--target", type=lambda s: int(s, 0), default=0xA5A5,
                    help="LUT mask to look for (default 0xA5A5)")
    ap.add_argument("--probe", metavar="X,Y,N",
                    help="also print the mask at this specific (X,Y,N)")
    ap.add_argument("--all-nonzero", action="store_true",
                    help="dump every position with mask != 0 and != 0xFFFF")
    args = ap.parse_args(argv)

    lib = load_so()
    with open(args.rbf, "rb") as fh:
        rbf = fh.read()
    print(f"scanning {args.rbf} ({len(rbf)} bytes) for mask=0x{args.target:04x}")

    if args.probe:
        px, py, pn = (int(s, 0) for s in args.probe.split(","))
        lc = LutCodec()
        st = lib.lc_init(ctypes.byref(lc), px, py, pn)
        if st != LC_OK:
            print(f"probe ({px},{py},{pn}): lc_init = {LC_STATUS_NAMES.get(st, st)}")
        else:
            m = read_mask_at(rbf, lc)
            print(f"probe ({px},{py},{pn}): base_addr=0x{lc.base_addr:08x} bp={lc.bp} "
                  f"sigma={list(lc.sigma)} k_odd={lc.k_odd} -> mask=0x{m:04x}")

    hits_target = []
    hits_nonzero = []
    scanned = 0
    for x in PIN_X:
        for y in LAB_Y:
            for n in EVEN_N:
                lc = LutCodec()
                st = lib.lc_init(ctypes.byref(lc), x, y, n)
                if st != LC_OK:
                    continue
                scanned += 1
                m = read_mask_at(rbf, lc)
                if m == args.target:
                    hits_target.append((x, y, n, m, lc.base_addr, lc.bp))
                if args.all_nonzero and m not in (0x0000, 0xFFFF):
                    hits_nonzero.append((x, y, n, m, lc.base_addr, lc.bp))

    print(f"scanned {scanned} pin-friendly positions")
    print(f"target mask 0x{args.target:04x} hits: {len(hits_target)}")
    for (x, y, n, m, ba, bp) in hits_target:
        print(f"  HIT (x={x:2d}, y={y:2d}, n={n:2d}) mask=0x{m:04x} "
              f"base_addr=0x{ba:08x} bp={bp}")

    if args.all_nonzero:
        print(f"non-trivial-mask positions (mask != 0x0000, 0xFFFF): {len(hits_nonzero)}")
        for (x, y, n, m, ba, bp) in hits_nonzero:
            print(f"  NZ  (x={x:2d}, y={y:2d}, n={n:2d}) mask=0x{m:04x} "
                  f"base_addr=0x{ba:08x} bp={bp}")


if __name__ == "__main__":
    main()
