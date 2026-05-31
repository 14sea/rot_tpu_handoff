#!/usr/bin/env python3
"""host_reference.py — golden reference for the Linux+TPU dense-layer classifier.

The TPU is a 4x4 weight-stationary int8 systolic array; from the RTL
(systolic_array_4x4.v / pe.v):
    RES[row] = sum_col W[row][col] * X[col]     (int8*int8 -> int32)
i.e. exactly a 4-input -> 4-output quantized dense layer. argmax(RES) = class.

This script is the single source of truth for the demo. It:
  1. defines the model W (4x4 int8) + a query X (4 int8),
  2. computes the expected RES0-3 and predicted class,
  3. computes the FNV-1a-32 hash of the 16 model bytes (the trust anchor the
     on-target init checks before loading weights into the TPU),
  4. emits tpu_model.bin (16 weight bytes + 4 input bytes) and a tampered copy,
  5. prints the C constants to bake into init.c.

Run:  python3 host_reference.py    (writes the .bin files next to it)
"""
from __future__ import annotations
import struct
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "initramfs"

# ── Model: 4 class-prototype weight rows (signed int8), row-major W[row][col] ──
# class 0 "low-weighted", 1 "high-weighted", 2 "alternating +", 3 "alternating -"
W = [
    [ 40,  30,  20,  10],   # class 0
    [ 10,  20,  30,  40],   # class 1
    [ 50, -50,  50, -50],   # class 2
    [-50,  50, -50,  50],   # class 3
]
# ── Query: a rising input vector (signed int8) ──
X = [1, 2, 3, 4]


def s8(b: int) -> int:
    """interpret a byte as signed int8"""
    return b - 256 if b >= 128 else b


def u8(v: int) -> int:
    """signed int8 value -> unsigned byte (two's complement)"""
    assert -128 <= v <= 127, v
    return v & 0xFF


def fnv1a32(data: bytes) -> int:
    h = 0x811C9DC5
    for byte in data:
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def compute_res(W, X):
    return [sum(W[r][c] * X[c] for c in range(4)) for r in range(4)]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    model_bytes = bytes(u8(W[r][c]) for r in range(4) for c in range(4))  # 16
    input_bytes = bytes(u8(v) for v in X)                                  # 4
    assert len(model_bytes) == 16 and len(input_bytes) == 4

    model_hash = fnv1a32(model_bytes)
    res = compute_res(W, X)
    cls = max(range(4), key=lambda r: res[r])

    # X_IN packed word {x3,x2,x1,x0}, x0 in bits[7:0]
    x_in_word = (u8(X[3]) << 24) | (u8(X[2]) << 16) | (u8(X[1]) << 8) | u8(X[0])

    # emit the model file (16 weights + 4 input) + a tampered copy
    (OUT / "tpu_model.bin").write_bytes(model_bytes + input_bytes)
    tampered = bytearray(model_bytes + input_bytes)
    tampered[0] ^= 0x01                          # flip one model bit
    (OUT / "tpu_model_tampered.bin").write_bytes(bytes(tampered))

    print("== model W (int8) ==")
    for r in range(4):
        print(f"  class {r}: {W[r]}")
    print(f"== query X = {X}  (X_IN word = 0x{x_in_word:08X}) ==")
    print("== expected RES[row] = sum_col W[row][col]*X[col] ==")
    for r in range(4):
        print(f"  RES[{r}] = {res[r]}")
    print(f"== predicted class = {cls}  (RES[{cls}]={res[cls]}) ==")
    print(f"== model FNV-1a-32 hash (16 bytes) = 0x{model_hash:08X} ==")
    print(f"== tampered hash = 0x{fnv1a32(bytes(tampered[:16])):08X} (must differ) ==")
    print()
    print("// ---- bake into init.c ----")
    print(f"#define TPU_MODEL_HASH 0x{model_hash:08X}u  /* FNV-1a-32 of 16 model bytes */")
    print(f"/* expected: class {cls}, RES = {res} */")
    print(f"wrote {OUT/'tpu_model.bin'} ({len(model_bytes)+len(input_bytes)} bytes), "
          f"{OUT/'tpu_model_tampered.bin'}")


if __name__ == "__main__":
    main()
