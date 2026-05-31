#!/usr/bin/env python3
"""test_canon_vs_golden.py — CONSUMER-side byte-identity acceptance gate.

Proves that the *on-chip* C codec (lutcodec.c / crc16.c, compiled verbatim
into lutcodec.so) plus the producer's D2 canon table
(canon_2input_codec_table_d1pp.json) reconstructs every Quartus golden RBF
byte-for-byte.

This is the consumer analogue of the producer's
upstream_ref/verify_canon_d1pp_table.py, with one decisive difference: the
σ⁻¹ ("predict_sram") step is realised by the **C** `lut_apply_mask` running
through ctypes — the exact bytes the NEORV32 would XOR in production — not by
the producer's Python LutCodec. So a green run proves:

    consumer_C_sigma_inv(pos, mask)  XOR  D2_canon[pos][mask]  XOR  CRC-repair
        == Quartus(pos, mask)      (modulo per-build header bytes)

i.e. the C σ⁻¹ math agrees with the producer reference on all 44 mined gold
points AND the canon + frame-CRC compose correctly.

RECONSTRUCTION (XOR is associative; double-application cancels, so applying
predict then canon == applying the producer's `predict ^ canon` symmetric
difference):
    buf = baseline_mask0
    lut_apply_mask(buf, pos, mask)            # C: XOR predict_sram cells
    for (addr,bit) in canon[pos][mask]: buf[addr] ^= 1<<bit   # XOR canon cells
    lc_patch_rbf_crc(buf)                     # C: recompute ALL frame CRCs
    assert buf == gold[pos][mask]             # excluding per-build header bytes

DISCRIMINATING CONTROLS (so a green run is not vacuous — see memory
feedback-discriminating-validation):
  * --check-no-canon : re-run WITHOUT the canon XOR. Expected to FAIL for the
    asymmetric masks — this is the empirical falsification of the old
    "canon-2input is a no-op at pin-friendly columns" claim baked into
    lutcodec.c's lut_apply_with_canon().
  * --negative-control : for the first entry, drop one canon cell. The
    reconstruction must then show diff_bytes>0, proving the comparison is
    sensitive to canon correctness.

SCOPE CAVEAT (inherited from the table): this is a lossless mine->verify
round-trip on the SAME builds the table was mined from — a table-integrity /
C-codec-parity / CRC gate, NOT a hold-out or silicon proof. It does NOT prove
the canon model generalises to unmined (pos,mask,N), and it does NOT prove
applying these (often far-flung lab_cram/block_band) canon cells onto the
*production iron* .rbf is runtime-safe. Asymmetric silicon validation remains
a separate, gated step.

Run:
    python3 test_canon_vs_golden.py
    EP4CE6_REPO=/home/test/EP4CE6 python3 test_canon_vs_golden.py --check-no-canon --negative-control
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]                      # .../rot_tpu_handoff
EP4CE6 = Path(os.environ.get("EP4CE6_REPO", "/home/test/EP4CE6"))
GOLDEN = EP4CE6 / "results" / "golden_rbf_modeH"
TABLE_PATH = HERE / "tables" / "canon_2input_codec_table_d1pp.json"

# Canonical consumer builder + its output (the verbatim on-chip C codec).
SO_BUILD = REPO / "docs/analysis/h1h2_silicon_test/host/build_lutcodec_so.sh"
SO_PATH = REPO / "docs/analysis/h1h2_silicon_test/host/lutcodec.so"

DIRTY_BITMAP_BYTES = 216  # CRC_DIRTY_BITMAP_BYTES = ceil(1727/8)
LC_OK = 0
LC_STATUS_NAMES = {0: "LC_OK", 1: "LC_ERR_X", 2: "LC_ERR_Y", 3: "LC_ERR_N"}


class LutCodec(ctypes.Structure):
    # Mirrors struct lut_codec_t (see edit_rbf.py / lutcodec.h).
    _fields_ = [
        ("base_addr", ctypes.c_uint32),
        ("sigma",     ctypes.c_uint8 * 4),
        ("bp",        ctypes.c_uint8),
        ("k_odd",     ctypes.c_uint8),
        ("x",         ctypes.c_uint16),
        ("y",         ctypes.c_uint16),
        ("n",         ctypes.c_uint16),
    ]


def build_and_load_so() -> ctypes.CDLL:
    if not SO_BUILD.exists():
        sys.exit(f"builder not found: {SO_BUILD}")
    subprocess.run(["bash", str(SO_BUILD)], check=True)
    lib = ctypes.CDLL(str(SO_PATH))
    lib.lc_init.restype = ctypes.c_int
    lib.lc_init.argtypes = [ctypes.POINTER(LutCodec),
                            ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint16]
    lib.lut_apply_mask.restype = None
    lib.lut_apply_mask.argtypes = [ctypes.POINTER(ctypes.c_uint8),
                                   ctypes.POINTER(LutCodec),
                                   ctypes.c_uint16,
                                   ctypes.POINTER(ctypes.c_uint8)]
    lib.lc_patch_rbf_crc.restype = None
    lib.lc_patch_rbf_crc.argtypes = [ctypes.POINTER(ctypes.c_uint8)]
    return lib


def parse_ranges(table) -> list[tuple[int, int]]:
    out = []
    for lo, hi in table.get("per_build_variable_ranges", [["0x29", "0x35"], ["0x49", "0x4B"]]):
        out.append((int(lo, 16), int(hi, 16)))
    return out


def in_per_build(addr: int, ranges) -> bool:
    return any(lo <= addr < hi for lo, hi in ranges)


def reconstruct(lib, base: bytes, x: int, y: int, n: int, mask: int,
                canon_cells, apply_canon: bool) -> tuple[bytes, str | None]:
    """Return (reconstructed_rbf, error_or_None)."""
    buf = bytearray(base)
    cbuf = (ctypes.c_uint8 * len(buf)).from_buffer(buf)
    lc = LutCodec()
    st = lib.lc_init(ctypes.byref(lc), x, y, n)
    if st != LC_OK:
        return bytes(buf), f"lc_init={LC_STATUS_NAMES.get(st, st)}"
    dirty = (ctypes.c_uint8 * DIRTY_BITMAP_BYTES)()
    lib.lut_apply_mask(cbuf, ctypes.byref(lc), mask, dirty)   # C σ⁻¹ predict_sram
    if apply_canon:
        for c in canon_cells:
            buf[int(c["addr"], 16)] ^= (1 << c["bit"])
    lib.lc_patch_rbf_crc(cbuf)                                # C: recompute all CRCs
    return bytes(buf), None


def diff_count(recon: bytes, gold: bytes, ranges) -> tuple[int, list[str]]:
    diffs = [i for i in range(len(gold))
             if recon[i] != gold[i] and not in_per_build(i, ranges)]
    return len(diffs), [f"0x{o:X}" for o in diffs[:5]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-no-canon", action="store_true",
                    help="also run a σ⁻¹-only pass; expected to FAIL (canon is load-bearing)")
    ap.add_argument("--negative-control", action="store_true",
                    help="drop one canon cell on the first entry; must then FAIL")
    args = ap.parse_args()

    if not GOLDEN.is_dir():
        sys.exit(f"golden RBF dir not found: {GOLDEN}\n"
                 f"set EP4CE6_REPO to the Cyclone_CRAM_Mapper checkout.")
    table = json.loads(TABLE_PATH.read_text())
    ranges = parse_ranges(table)
    lib = build_and_load_so()

    entries = []   # (x,y,n,mask_s,canon_cells)
    for key, pos in table["per_position"].items():
        x, y, n = pos["x"], pos["y"], pos["n"]
        for mask_s, entry in pos["masks"].items():
            entries.append((x, y, n, mask_s, entry["cells"]))

    def gold_path(x, y, n, mask_s):
        return GOLDEN / f"X{x}_Y{y}_N{n}_mask{mask_s[2:].upper()}_q211.rbf"

    def base_path(x, y, n):
        return GOLDEN / f"X{x}_Y{y}_N{n}_mask0000_q211.rbf"

    # ---- Primary gate: σ⁻¹ + canon ----
    print("== σ⁻¹ (C codec) + D2 canon  vs  Quartus golds ==")
    n_pass = 0
    for (x, y, n, mask_s, cells) in entries:
        base = base_path(x, y, n).read_bytes()
        gold = gold_path(x, y, n, mask_s).read_bytes()
        recon, err = reconstruct(lib, base, x, y, n, int(mask_s, 16), cells, apply_canon=True)
        if err:
            print(f"  [FAIL] X{x}Y{y}N{n} {mask_s}: {err}")
            continue
        d, first = diff_count(recon, gold, ranges)
        tag = "PASS" if d == 0 else "FAIL"
        if d == 0:
            n_pass += 1
        else:
            print(f"  [{tag}] X{x}Y{y}N{n} {mask_s}: diff_bytes={d} {first}")
    print(f"  => {n_pass}/{len(entries)} byte-identical (excl. per-build header bytes)")

    # ---- Discriminating control 1: σ⁻¹ only (no canon) ----
    no_canon_fail = None
    if args.check_no_canon:
        print("\n== σ⁻¹ ONLY, no canon  (expected to FAIL — proves canon is NOT a no-op) ==")
        nc_pass = 0
        for (x, y, n, mask_s, cells) in entries:
            base = base_path(x, y, n).read_bytes()
            gold = gold_path(x, y, n, mask_s).read_bytes()
            recon, err = reconstruct(lib, base, x, y, n, int(mask_s, 16), cells, apply_canon=False)
            d, _ = diff_count(recon, gold, ranges)
            if d == 0:
                nc_pass += 1
        no_canon_fail = len(entries) - nc_pass
        print(f"  => {nc_pass}/{len(entries)} pass WITHOUT canon "
              f"({no_canon_fail} fail → canon carries real per-position cells)")

    # ---- Discriminating control 2: corrupt one canon cell ----
    neg_ok = None
    if args.negative_control:
        print("\n== negative control: drop 1 canon cell on first entry (must FAIL) ==")
        x, y, n, mask_s, cells = entries[0]
        base = base_path(x, y, n).read_bytes()
        gold = gold_path(x, y, n, mask_s).read_bytes()
        recon, _ = reconstruct(lib, base, x, y, n, int(mask_s, 16), cells[:-1], apply_canon=True)
        d, first = diff_count(recon, gold, ranges)
        neg_ok = d > 0
        print(f"  X{x}Y{y}N{n} {mask_s} with 1 canon cell removed: diff_bytes={d} {first} "
              f"-> {'OK (gate discriminates)' if neg_ok else 'BROKEN (gate is vacuous!)'}")

    # ---- Verdict ----
    ok = (n_pass == len(entries))
    if args.check_no_canon:
        ok = ok and (no_canon_fail is not None and no_canon_fail > 0)
    if args.negative_control:
        ok = ok and bool(neg_ok)
    print(f"\n[scope] lossless mine->verify round-trip; consumer-C-codec + canon parity gate, "
          f"NOT a hold-out or silicon proof.")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
