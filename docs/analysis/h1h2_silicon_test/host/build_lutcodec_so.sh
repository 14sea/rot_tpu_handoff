#!/usr/bin/env bash
# build_lutcodec_so.sh -- compile the on-chip lutcodec + crc16 sources into
# a host shared library that edit_rbf.py loads via ctypes.
#
# Sources live in the scratch_merge/sw/stage2_loader/ tree and are used
# verbatim -- same lc_init / lut_apply_mask / lc_patch_rbf_crc_frames code
# that runs on the NEORV32 in production. This guarantees the host editor
# realizes the same XOR pattern the on-chip path would, so a failing H1/H2
# verdict is a property of the math/silicon, not a host-vs-target drift.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
SRC="$HERE/../../plan_c1_le/scratch_merge/sw/stage2_loader"

if [[ ! -d "$SRC" ]]; then
    echo "lutcodec source dir not found: $SRC" >&2
    exit 1
fi

OUT="$HERE/lutcodec.so"

gcc -O2 -fPIC -shared \
    -I "$SRC" \
    "$SRC/lutcodec.c" \
    "$SRC/lutcodec_data.c" \
    "$SRC/crc16.c" \
    -o "$OUT"

echo "built: $OUT"
