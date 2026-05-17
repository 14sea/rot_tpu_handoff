#!/usr/bin/env python3
# scripts/nh1_mode_p_test.py — LEGACY (NH1 era, 2026-05-17 night)
#
# ⚠️  DEPRECATED — kept for historical reference only.
#
# Original purpose: validate patch 0009 (Phase 3 NH1 wait_not_busy
# two-phase fix) on the *production* mode 'P' code path which prints
# "[stage2] Mode: EPCS probe — read 64 B @0x000000", expects 4 hex
# rows + "probe DONE" as success markers.
#
# Why deprecated:
#   - NH1 was refuted (3/3 byte-identical hang) and patch 0009 was
#     retained only as defensive.
#   - This script's EXPECTED_OLD_MD5 hardcodes the original pre-NH1
#     bitstream md5 fcfd8122 and refuses to test anything newer.
#   - The production mode 'P' code path itself hangs in wait_not_busy
#     because the qmegawiz altasmi_parallel READ FSM has a structural
#     deadlock — see docs/captures/diag_baseline_hang.log and patches
#     0011-0013 commit messages for the full investigation.
#   - All subsequent investigation switched to the diagnostic mode 'P'
#     introduced by patch 0010 (STATUS-trace), which has its own
#     listener wrapper at scripts/diag_mode_p_test.py.
#
# Use scripts/diag_mode_p_test.py for any current testing.
# This file is kept so prior commit messages that reference it still
# resolve, and so the original NH1 test methodology is preserved as
# a reasoning trail.
#
# Verdicts (also reflected in exit code):
#   0 NH1_CONFIRMED      entry banner + 4 hex rows + RDSR + "probe DONE"
#   1 NH1_REFUTED_HANG   entry banner seen but no "probe DONE" within timeout
#   2 PRECONDITION_FAIL  device missing, bitstream md5 unchanged, etc.
#   3 NO_BANNER          stage2 banner never appeared post-flash
#   4 NO_DISPATCHER      banner seen but "CRTM locked" never reached

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial

DEV          = "/dev/ttyUSB0"
BAUD         = 115200
BITSTREAM    = "/home/test/neorv32_rot/output/neorv32_demo.rbf"
STAGE2_ELF   = "/home/test/neorv32_rot/sw/stage2_loader/main.elf"
EXPECTED_OLD_MD5 = "fcfd8122bd4772940f3706c88b51cef3"  # pre-NH1 bitstream
# Project-local openFPGALoader build (system /usr/bin one lacks EP4CE10
# IDCODE 0x020f10dd in its device DB).  Matches Makefile OFL_LOADER var.
OFL_LOADER   = os.path.expanduser(
    "~/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader")

BANNER_RE     = re.compile(rb"\[stage2\]")
CRTM_LOCKED   = b"[stage2] CRTM locked"
PROBE_ENTRY   = b"[stage2] Mode: EPCS probe"
PROBE_DONE    = b"probe DONE"
HEX_ROW_RE    = re.compile(rb"^[0-9a-fA-F]{8}:( [0-9a-fA-F]{2}){16}\r?$", re.MULTILINE)


class Listener(threading.Thread):
    """Read /dev/ttyUSB0 in background, tee to log file + stdout, expose buf."""

    def __init__(self, dev: str, baud: int, log_path: Path):
        super().__init__(daemon=True)
        self.dev = dev
        self.baud = baud
        self.log_path = log_path
        self.buf = bytearray()
        self.buf_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.ser: serial.Serial | None = None

    def open_port(self):
        self.ser = serial.Serial(self.dev, self.baud, timeout=0.1)

    def run(self):
        assert self.ser is not None
        with open(self.log_path, "wb") as f:
            while not self.stop_flag.is_set():
                try:
                    chunk = self.ser.read(256)
                except Exception as e:
                    print(f"[listener] read error: {e}", file=sys.stderr)
                    break
                if chunk:
                    with self.buf_lock:
                        self.buf.extend(chunk)
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.flush()
                    f.write(chunk)
                    f.flush()

    def stop(self):
        self.stop_flag.set()
        if self.ser:
            self.ser.close()

    def snapshot(self) -> bytes:
        with self.buf_lock:
            return bytes(self.buf)

    def wait_for(self, marker: bytes, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if marker in self.snapshot():
                return True
            time.sleep(0.05)
        return False

    def send(self, data: bytes):
        assert self.ser is not None
        self.ser.write(data)
        self.ser.flush()


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight() -> tuple[str, int]:
    print("=== Pre-flight ===", flush=True)
    if not os.path.exists(DEV):
        print(f"  [FAIL] {DEV} not present", flush=True)
        sys.exit(2)
    if not os.path.exists(OFL_LOADER):
        print(f"  [FAIL] project-local openFPGALoader not found at {OFL_LOADER}", flush=True)
        sys.exit(2)
    if not os.path.exists(BITSTREAM):
        print(f"  [FAIL] bitstream missing: {BITSTREAM}", flush=True)
        sys.exit(2)

    bs_md5 = md5(BITSTREAM)
    bs_size = os.path.getsize(BITSTREAM)
    print(f"  bitstream  : {BITSTREAM}", flush=True)
    print(f"  bs md5     : {bs_md5} ({bs_size} B)", flush=True)
    if bs_md5 == EXPECTED_OLD_MD5:
        print("  [WARN] bitstream md5 == pre-NH1 baseline — stage2 NH1 fix is NOT", flush=True)
        print("         baked in.  Rebuild bitstream first:", flush=True)
        print("         (cd .. && make rot-bitstream)", flush=True)
        print("         Aborting to avoid misleading test result.", flush=True)
        sys.exit(2)

    if os.path.exists(STAGE2_ELF):
        elf_size = os.path.getsize(STAGE2_ELF)
        print(f"  stage2 ELF : {STAGE2_ELF} ({elf_size} B)", flush=True)
    print(f"  device     : {DEV} @ {BAUD} 8N1", flush=True)
    return bs_md5, bs_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/tmp/nh1_uart.log", help="raw UART log path")
    ap.add_argument("--boot-timeout", type=float, default=15.0)
    ap.add_argument("--probe-timeout", type=float, default=15.0)
    ap.add_argument("--no-flash", action="store_true", help="skip openFPGALoader; assume FPGA already running")
    args = ap.parse_args()

    bs_md5, _ = preflight()
    log_path = Path(args.log)
    if log_path.exists():
        log_path.unlink()

    listener = Listener(DEV, BAUD, log_path)
    print("\n=== Listener-first ===", flush=True)
    listener.open_port()
    listener.start()
    time.sleep(0.3)  # let thread settle
    print(f"  capturing to {log_path}", flush=True)

    if not args.no_flash:
        print("\n=== Flash ===", flush=True)
        # openFPGALoader can't share the serial port directly, but it
        # talks to USB-Blaster (separate USB endpoint).  Listener stays
        # open on /dev/ttyUSB0 (PL2303-style serial bridge).
        cmd = [OFL_LOADER, "-c", "usb-blaster", "-m", BITSTREAM]
        print(f"  $ {' '.join(cmd)}", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"  [FAIL] openFPGALoader rc={r.returncode}", flush=True)
            print(f"  stderr: {r.stderr[-500:]}", flush=True)
            listener.stop()
            sys.exit(2)
        print("  flash OK", flush=True)
    else:
        print("\n=== Skipping flash (--no-flash) ===", flush=True)

    print("\n=== Wait for stage2 boot ===", flush=True)
    if not listener.wait_for(b"[stage2]", args.boot_timeout):
        print(f"  [FAIL] no stage2 banner within {args.boot_timeout}s", flush=True)
        listener.stop()
        sys.exit(3)
    print("  banner seen", flush=True)

    if not listener.wait_for(CRTM_LOCKED, args.boot_timeout):
        print(f"  [FAIL] CRTM locked marker not seen within {args.boot_timeout}s", flush=True)
        listener.stop()
        sys.exit(4)
    print("  CRTM locked → dispatcher ready", flush=True)

    print("\n=== Send 'P' ===", flush=True)
    time.sleep(0.2)  # let dispatcher settle
    listener.send(b"P")
    print("  'P' sent", flush=True)

    print("\n=== Wait for probe DONE ===", flush=True)
    saw_entry = listener.wait_for(PROBE_ENTRY, 2.0)
    if not saw_entry:
        print("  [FAIL] mode 'P' entry banner not seen — dispatcher may have ignored 'P'", flush=True)
        listener.stop()
        sys.exit(4)
    print("  entry banner seen", flush=True)

    saw_done = listener.wait_for(PROBE_DONE, args.probe_timeout)
    listener.stop()
    time.sleep(0.2)
    listener.join(timeout=1.0)

    print("\n=== Verdict ===", flush=True)
    final = listener.snapshot()
    n_hex_rows = len(HEX_ROW_RE.findall(final))
    print(f"  entry banner  : {'YES' if saw_entry else 'NO'}", flush=True)
    print(f"  hex rows      : {n_hex_rows} (expect 4)", flush=True)
    print(f"  probe DONE    : {'YES' if saw_done else 'NO'}", flush=True)
    print(f"  log saved at  : {log_path} ({log_path.stat().st_size} B)", flush=True)

    if saw_done and n_hex_rows == 4:
        print("\n  ✓ NH1_CONFIRMED — mode 'P' completed normally.", flush=True)
        print("    Patch 0009 fix is correct; Phase 3 silicon validation closes.", flush=True)
        sys.exit(0)
    elif saw_entry and not saw_done:
        print("\n  ✗ NH1_REFUTED_HANG — entry banner seen but probe never finished.", flush=True)
        print("    The wait_not_busy two-phase fix did NOT unblock the hang.", flush=True)
        print("    Move debug to NH2 (rublock-vs-asmiblock arch contention)", flush=True)
        print("    or NH3 (JTAG-volatile vs EPCS-cold-boot pin state).", flush=True)
        sys.exit(1)
    else:
        print("\n  ? INCONCLUSIVE — partial output captured.  Review log.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        sys.exit(130)
