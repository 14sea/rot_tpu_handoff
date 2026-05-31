#!/usr/bin/env python3
"""analyze_iron_transfer.py — caveat #2 static analysis.

Question: is it safe to XOR a mode-H asymmetric edit (σ⁻¹ + D2 canon, mined
from a MINIMAL one-LUT design) onto the FULL production iron .rbf?

The net delta mode H applies = predict_sram(M) △ canon[pos][M]  (symmetric
difference; double-XOR cancels), which by construction equals
(gold_maskM XOR gold_mask0) on the touched cells. So:

  production_new = production XOR delta

NECESSARY safety gate (collision check): for every touched cell, production
must already hold the minimal-design mask=0 baseline value, i.e.
    production[cell] == gold_mask0[cell]   for all cell in delta.
If production differs at a touched cell, that cell is occupied by other
production logic and XOR-flipping it corrupts that logic (and yields the wrong
canon state). 0 collisions ⟺ production is in the empty-baseline state at
every cell the edit touches.

NOT sufficient on its own: a touched cell that is SHARED (value depends on
neighbouring logic) but coincidentally matches the baseline would pass the
collision check yet still be wrong once flipped. So we also report spatial
LOCALITY (distance from base_addr) and REGION (lut_data / lab_cram /
block_band). block_band cells are block-level (shared) and are the
highest-risk class even at 0 collisions.

Pure host analysis. No flash. Reads production iron from a bit-reversed EPCS
backup; golds from $EP4CE6_REPO.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
EP4CE6 = Path(os.environ.get("EP4CE6_REPO", "/home/test/EP4CE6"))
GOLDEN = EP4CE6 / "results" / "golden_rbf_modeH"
TABLE_PATH = HERE / "tables" / "canon_2input_codec_table_d1pp.json"
SO_BUILD = REPO / "docs/analysis/h1h2_silicon_test/host/build_lutcodec_so.sh"
SO_PATH = REPO / "docs/analysis/h1h2_silicon_test/host/lutcodec.so"
PROD_RBF = Path(os.environ.get("PROD_RBF", "/tmp/prod_iron_page0.rbf"))
# Authoritative production iron (Plan C-1 v4, NO mode H): page-0 .rbf md5
# fd884770, derived by bit-reversing the in-repo pre-mode-H EPCS backup.
EPCS_BACKUP = REPO / "docs/safety/epcs_backup_20260521_pre_modeH.bin"

PAIR_SPACING = 210
EDIT_LO, EDIT_HI = 5282, 367952   # D4: CRAM frames 25..1751


class LutCodec(ctypes.Structure):
    _fields_ = [("base_addr", ctypes.c_uint32), ("sigma", ctypes.c_uint8 * 4),
                ("bp", ctypes.c_uint8), ("k_odd", ctypes.c_uint8),
                ("x", ctypes.c_uint16), ("y", ctypes.c_uint16), ("n", ctypes.c_uint16)]


def load_so():
    subprocess.run(["bash", str(SO_BUILD)], check=True, stdout=subprocess.DEVNULL)
    lib = ctypes.CDLL(str(SO_PATH))
    lib.lc_init.restype = ctypes.c_int
    lib.lc_init.argtypes = [ctypes.POINTER(LutCodec),
                            ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint16]
    return lib


def init(lib, x, y, n):
    lc = LutCodec()
    if lib.lc_init(ctypes.byref(lc), x, y, n) != 0:
        return None
    return lc


def predict_cells(lc, mask):
    """σ⁻¹ data cells flipped for `mask` (mirror of lut_apply_mask)."""
    s = list(lc.sigma)
    cells = set()
    for b in range(16):
        if not (mask >> b) & 1:
            continue
        f = [(b >> s[i]) & 1 for i in range(4)]
        pair = ((1 - f[0]) << 2) | ((1 - f[1]) << 1) | (1 - f[2])
        da = f[3] ^ (1 - f[2])
        delta = (1 - da) if lc.k_odd else da
        cells.add((lc.base_addr + pair * PAIR_SPACING + delta, lc.bp))
    return cells


def read_mask(rbf, lc):
    m = 0
    for b, (addr, bit) in _ordered_minterm_cells(lc):
        if addr < len(rbf) and (rbf[addr] >> bit) & 1:
            m |= (1 << b)
    return m


def _ordered_minterm_cells(lc):
    s = list(lc.sigma)
    for b in range(16):
        f = [(b >> s[i]) & 1 for i in range(4)]
        pair = ((1 - f[0]) << 2) | ((1 - f[1]) << 1) | (1 - f[2])
        da = f[3] ^ (1 - f[2])
        delta = (1 - da) if lc.k_odd else da
        yield b, (lc.base_addr + pair * PAIR_SPACING + delta, lc.bp)


def is_crc_byte(off):
    return (off - 32) % PAIR_SPACING >= 208


def bit(buf, addr, b):
    return (buf[addr] >> b) & 1


def main():
    if not PROD_RBF.exists():
        # Regenerate from the in-repo EPCS backup: bitrev(first 368011 bytes).
        if not EPCS_BACKUP.exists():
            sys.exit(f"neither {PROD_RBF} nor backup {EPCS_BACKUP} found")
        br = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))
        page0 = EPCS_BACKUP.read_bytes()[:368011].translate(br)
        PROD_RBF.write_bytes(page0)
        print(f"derived {PROD_RBF} from {EPCS_BACKUP.name} (bit-reversed page 0)")
    if not GOLDEN.is_dir():
        sys.exit(f"golden dir not found: {GOLDEN} (set EP4CE6_REPO)")
    prod = PROD_RBF.read_bytes()
    table = json.loads(TABLE_PATH.read_text())
    lib = load_so()

    print(f"production iron : {PROD_RBF} ({len(prod)} B)")
    print(f"editable range  : [{EDIT_LO}, {EDIT_HI})  (D4 frames 25..1751)\n")

    # ---- Pass 1: which covered positions are mask=0x0000 in production iron ----
    print("== covered positions: current production-iron mask + data-cell baseline match ==")
    print("  pos          prod_mask  gold0_mask  data16_match  base_addr   bp")
    candidates = []
    region_canon = {}   # (x,y,n,mask_s) -> {(addr,bit): region}
    for key, pos in table["per_position"].items():
        x, y, n = pos["x"], pos["y"], pos["n"]
        lc = init(lib, x, y, n)
        gold0 = (GOLDEN / f"X{x}_Y{y}_N{n}_mask0000_q211.rbf").read_bytes()
        pmask = read_mask(prod, lc)
        gmask = read_mask(gold0, lc)
        data_cells = [c for _, c in _ordered_minterm_cells(lc)]
        d16 = all(bit(prod, a, b) == bit(gold0, a, b) for a, b in data_cells)
        flag = "  <- candidate" if (pmask == 0 and d16 and y != 2) else ("  (Y=2 header)" if y == 2 else "")
        print(f"  X{x}Y{y}N{n:<2}     0x{pmask:04X}     0x{gmask:04X}      "
              f"{str(d16):<5}        0x{lc.base_addr:06X}  {lc.bp}{flag}")
        if pmask == 0 and d16 and y != 2:
            candidates.append((x, y, n))

    # ---- Pass 2: collision matrix over every (pos,mask) ----
    print("\n== transfer collision matrix (delta = predict △ canon, vs gold_mask0 baseline) ==")
    print("  collisions = touched cells where production != minimal-baseline (UNSAFE to XOR)")
    print(f"  {'pos':<11}{'mask':<8}{'Δcells':<8}{'collide':<9}{'  region breakdown of collisions'}")
    matrix = {}
    for key, pos in table["per_position"].items():
        x, y, n = pos["x"], pos["y"], pos["n"]
        lc = init(lib, x, y, n)
        gold0 = (GOLDEN / f"X{x}_Y{y}_N{n}_mask0000_q211.rbf").read_bytes()
        for mask_s, entry in pos["masks"].items():
            M = int(mask_s, 16)
            pred = predict_cells(lc, M)
            canon = {}
            for c in entry["cells"]:
                canon[(int(c["addr"], 16), c["bit"])] = c["region"]
            canon_set = set(canon.keys())
            delta = pred ^ canon_set          # symmetric difference = net flipped cells
            region_canon[(x, y, n, mask_s)] = canon
            # collisions
            coll = []
            reg_count = {}
            for (a, b) in delta:
                if bit(prod, a, b) != bit(gold0, a, b):
                    coll.append((a, b))
                    r = canon.get((a, b), "lut_data")
                    reg_count[r] = reg_count.get(r, 0) + 1
            matrix[(x, y, n, mask_s)] = (len(delta), len(coll), reg_count)
            rb = " ".join(f"{r}:{c}" for r, c in sorted(reg_count.items())) or "—"
            print(f"  X{x}Y{y}N{n:<2}    {mask_s:<8}{len(delta):<8}{len(coll):<9}  {rb}")

    clean = [k for k, (_, c, _) in matrix.items() if c == 0]
    print(f"\n  clean (0-collision) (pos,mask) transfers: {len(clean)}/{len(matrix)}")
    for k in clean:
        print(f"    {k}")

    # ---- Pass 3: deep-dive ----
    target = None
    if clean:
        target = clean[0]
    elif candidates:
        x, y, n = candidates[0]
        for mask_s in table["per_position"][f"{x},{y},{n}"]["masks"]:
            target = (x, y, n, mask_s); break
    if target:
        x, y, n, mask_s = target
        print(f"\n== deep-dive: X{x}Y{y}N{n} mask {mask_s} ==")
        lc = init(lib, x, y, n)
        gold0 = (GOLDEN / f"X{x}_Y{y}_N{n}_mask0000_q211.rbf").read_bytes()
        pred = predict_cells(lc, int(mask_s, 16))
        canon = region_canon[(x, y, n, mask_s)]
        delta = sorted(pred ^ set(canon.keys()))
        base = lc.base_addr
        # locality + range + crc + collision per cell
        in_range = all(EDIT_LO <= a < EDIT_HI for a, _ in delta)
        any_crc = any(is_crc_byte(a) for a, _ in delta)
        dists = sorted(abs(a - base) for a, _ in delta)
        loc_local = sum(1 for d in dists if d <= 1680)       # within the 8-frame LE window
        regions = {}
        for (a, b) in delta:
            r = canon.get((a, b), "lut_data")
            regions[r] = regions.get(r, 0) + 1
        print(f"  base_addr=0x{base:06X} bp={lc.bp}  Δ={len(delta)} cells")
        print(f"  region breakdown : {regions}")
        print(f"  all in editable range [5282,367952): {in_range}   any CRC-byte cell: {any_crc}")
        print(f"  locality: {loc_local}/{len(delta)} within ±1680 B (LE 8-frame window); "
              f"max dist from base = {dists[-1]} B ({dists[-1]//PAIR_SPACING} frames)")
        print(f"  D3 FF-avoid: no per-LE FF CRAM bit exists (cert) — but D3 covered only the "
              f"16 minterm cells, NOT these canon cells.")
        print(f"\n  sample of farthest cells (region, dist_frames, prod_bit, gold0_bit):")
        far = sorted(delta, key=lambda ab: -abs(ab[0] - base))[:8]
        for (a, b) in far:
            r = canon.get((a, b), "lut_data")
            print(f"    0x{a:06X}.{b} {r:<11} dist={abs(a-base)//PAIR_SPACING:>4}f "
                  f"prod={bit(prod,a,b)} gold0={bit(gold0,a,b)} "
                  f"{'COLLIDE' if bit(prod,a,b)!=bit(gold0,a,b) else 'ok'}")


if __name__ == "__main__":
    main()
