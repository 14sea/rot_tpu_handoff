#!/usr/bin/env python3
# scripts/diag_mode_p_test.py
#
# Mode 'P' STATUS-trace diagnostic HW test wrapper.  Pairs with patch
# 0010 (diagnostic mode_epcs_probe).  Forked from nh1_mode_p_test.py:
# the NH1 wrapper expected the production "4 hex rows + probe DONE"
# output; this one parses the [P] tag STATUS trace and classifies
# NH2/NH3/NH4/NH5.
#
# Listener-first ordering per [[feedback-uart-listener-first]].
# Uses the project-local openFPGALoader build (system one lacks
# EP4CE10 IDCODE 0x020f10dd).
#
# Verdicts (also reflected in exit code):
#   0  SUCCESS_UNEXPECTED   Phase B completed (DV asserted + DATAOUT) —
#                           diagnostic itself dodged the hang.  Re-audit
#                           epcs.c vs the inline code.
#   1  CLASSIFIED           trace captured + classified (one of NH2..5).
#   2  PRECONDITION_FAIL    device missing, bitstream wrong, etc.
#   3  NO_BANNER            stage2 banner never appeared post-flash.
#   4  NO_DISPATCHER        banner seen but dispatcher never reached.
#   5  NO_DIAG_ENTRY        sent 'P' but [P] tag never appeared.
#   6  INCOMPLETE_TRACE     diagnostic started but timed out mid-trace.

import argparse
import hashlib
import os
import re
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
# Diagnostic + NH6 bitstreams known to embed patch 0010's instrumented
# mode 'P'.  Prefix match is heuristic (any rebuild produces a new md5).
EXPECTED_DIAG_MD5_PREFIXES = ("f931e805", "aa41f714", "011a4c37", "bdee9496", "891efece", "1787cd8a", "df477575", "5145a6d7", "43f8578e", "bc9ba066")
OFL_LOADER   = os.path.expanduser(
    "~/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader")

BANNER_MARK   = b"[stage2]"
CRTM_LOCKED   = b"[stage2] CRTM locked"
DIAG_ENTRY    = b"[P] diagnostic STATUS-trace probe"
DIAG_DONE     = b"[P] diagnostic DONE"

# Captures lines like "[P] A2 wait_not_busy i=0x00000010 STATUS=0x00000001"
LINE_RE = re.compile(
    rb"\[P\]\s+([A-Za-z0-9]+)(.*?)STATUS=0x([0-9a-fA-F]+)",
    re.MULTILINE,
)
TIMEOUT_RE = re.compile(rb"\[P\]\s+([A-Za-z0-9]+)\s+done.*?final=0x([0-9a-fA-F]+)\s+TIMEOUT")
OK_RE      = re.compile(rb"\[P\]\s+([A-Za-z0-9]+)\s+done.*?final=0x([0-9a-fA-F]+)\s+OK")
DATAOUT_RE = re.compile(rb"\[P\]\s+B5\s+DATAOUT=0x([0-9a-fA-F]+)")
# B1 after CTRL=READ STATUS=0xNN — discriminates rden=1 deadlock (STATUS=0x03
# immediately) from rden=0 FSM-abort variant (STATUS=0x00, no busy ever).
B1_RE      = re.compile(rb"\[P\]\s+B1\s+after\s+CTRL=READ\s+STATUS=0x([0-9a-fA-F]+)")
# B5b/B5c added by patch 0013 NH8 v3 diagnostic firmware — capture the
# state right after the firmware-issued rden_drop pulse.
B5B_RE     = re.compile(rb"\[P\]\s+B5b\s+after\s+CTRL=READ\|RDEN_DROP\s+STATUS=0x([0-9a-fA-F]+)")
B5C_RE     = re.compile(rb"\[P\]\s+B5c\s+post_rden_drop_wait.*?final=0x([0-9a-fA-F]+)\s+(OK|TIMEOUT)")
# Phase C (NH9 FAST_READ): up to 4 bytes; each line either ends in
# DATAOUT=0xNN (success) or TIMEOUT.
C2_BYTE_RE = re.compile(
    rb"\[P\]\s+C2\s+byte=0x([0-9a-fA-F]+)\s+wait_dv\s+i=0x([0-9a-fA-F]+)\s+STATUS=0x([0-9a-fA-F]+)\s+(DATAOUT=0x([0-9a-fA-F]+)|TIMEOUT)"
)
C4_RE      = re.compile(rb"\[P\]\s+C4\s+post_rden_drop_wait.*?final=0x([0-9a-fA-F]+)\s+(OK|TIMEOUT)")


class Listener(threading.Thread):
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
        # Set the flag and let run() exit naturally on the next loop
        # iteration (within ~100 ms via ser.read timeout).  Closing
        # self.ser here would race the in-flight read() and produce
        # "'NoneType' object cannot be interpreted as an integer".
        self.stop_flag.set()

    def close(self):
        # Call AFTER join() to release the serial port without racing
        # the listener thread.
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

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


def preflight():
    print("=== Pre-flight ===", flush=True)
    for p in [DEV, OFL_LOADER, BITSTREAM]:
        if not os.path.exists(p):
            print(f"  [FAIL] missing: {p}", flush=True)
            sys.exit(2)
    bs_md5 = md5(BITSTREAM)
    print(f"  bitstream  : {BITSTREAM}", flush=True)
    print(f"  bs md5     : {bs_md5} ({os.path.getsize(BITSTREAM)} B)", flush=True)
    if not any(bs_md5.startswith(p) for p in EXPECTED_DIAG_MD5_PREFIXES):
        print(f"  [WARN] bitstream md5 prefix not in {EXPECTED_DIAG_MD5_PREFIXES}", flush=True)
        print(f"         current bitstream may not be the diagnostic build.", flush=True)
        print(f"         Rebuild: cd .. && make rot-bitstream", flush=True)
        print(f"         (continuing anyway — md5 is heuristic across rebuilds)", flush=True)
    if os.path.exists(STAGE2_ELF):
        print(f"  stage2 ELF : {STAGE2_ELF} ({os.path.getsize(STAGE2_ELF)} B)", flush=True)
    print(f"  device     : {DEV} @ {BAUD} 8N1", flush=True)


def classify(buf: bytes) -> tuple[str, list[str]]:
    """Return (verdict_tag, lines_of_reasoning) for the captured trace."""
    reasoning = []

    # Were illegal_erase / illegal_write ever observed?
    seen_ill = False
    for m in LINE_RE.finditer(buf):
        s = int(m.group(3), 16)
        if s & 0x4 or s & 0x8:
            seen_ill = True
            reasoning.append(
                f"  STATUS shows illegal bit set: tag={m.group(1).decode()}, "
                f"STATUS=0x{s:08x}"
            )
            break

    # Did Phase A finish?
    a2_to = re.search(rb"\[P\] A2 done.*?final=0x([0-9a-fA-F]+)\s+(OK|TIMEOUT)", buf)
    b1    = B1_RE.search(buf)
    b2_to = re.search(rb"\[P\] B2 done.*?final=0x([0-9a-fA-F]+)\s+(OK|TIMEOUT)", buf)
    b4_to = re.search(rb"\[P\] B4 done.*?final=0x([0-9a-fA-F]+)\s+(OK|TIMEOUT)", buf)
    b5    = DATAOUT_RE.search(buf)
    b5b   = B5B_RE.search(buf)            # NH8 v3 trace only
    b5c_m = B5C_RE.search(buf)            # NH8 v3 trace only

    a2_status = (a2_to.group(2).decode() if a2_to else "ABSENT")
    a2_final  = (int(a2_to.group(1), 16) if a2_to else None)
    b1_status = (int(b1.group(1), 16) if b1 else None)
    b2_status = (b2_to.group(2).decode() if b2_to else "ABSENT")
    b2_final  = (int(b2_to.group(1), 16) if b2_to else None)
    b4_status = (b4_to.group(2).decode() if b4_to else "ABSENT")
    b4_final  = (int(b4_to.group(1), 16) if b4_to else None)
    b5b_status = (int(b5b.group(1), 16) if b5b else None)
    b5c_status = (b5c_m.group(2).decode() if b5c_m else None)
    b5c_final  = (int(b5c_m.group(1), 16) if b5c_m else None)

    reasoning.append(f"  Phase A (RDSR) wait_not_busy: {a2_status}"
                     + (f", final=0x{a2_final:08x}" if a2_final is not None else ""))
    if b1_status is not None:
        reasoning.append(f"  B1 after CTRL=READ STATUS  : 0x{b1_status:08x}")
    reasoning.append(f"  Phase B pre-amble wait     : {b2_status}"
                     + (f", final=0x{b2_final:08x}" if b2_final is not None else ""))
    reasoning.append(f"  Phase B dv_wait            : {b4_status}"
                     + (f", final=0x{b4_final:08x}" if b4_final is not None else ""))
    if b5:
        reasoning.append(f"  B5 DATAOUT=0x{int(b5.group(1), 16):08x}")
    if b5b_status is not None:
        reasoning.append(f"  B5b post-RDEN_DROP STATUS  : 0x{b5b_status:08x}")
    if b5c_status is not None:
        reasoning.append(f"  B5c post-rden-drop wait    : {b5c_status}"
                         + (f", final=0x{b5c_final:08x}" if b5c_final is not None else ""))

    # ── Phase C (NH9 FAST_READ regen) — takes precedence when present ──
    c2_bytes = list(C2_BYTE_RE.finditer(buf))
    c4_m     = C4_RE.search(buf)
    if c2_bytes:
        decoded = []
        any_timeout = False
        for m in c2_bytes:
            byte_i  = int(m.group(1), 16)
            iters   = int(m.group(2), 16)
            status  = int(m.group(3), 16)
            if m.group(4).startswith(b"DATAOUT="):
                dv = int(m.group(5), 16) & 0xff
                decoded.append((byte_i, iters, status, dv))
                reasoning.append(
                    f"  C2 byte {byte_i}: DATAOUT=0x{dv:02x} (wait i=0x{iters:x}, STATUS=0x{status:08x})"
                )
            else:
                any_timeout = True
                reasoning.append(
                    f"  C2 byte {byte_i}: TIMEOUT (wait i=0x{iters:x}, STATUS=0x{status:08x})"
                )
                break
        c4_status = (c4_m.group(2).decode() if c4_m else "ABSENT")
        c4_final  = (int(c4_m.group(1), 16) if c4_m else None)
        if c4_m:
            reasoning.append(f"  C4 post-RDEN_DROP wait: {c4_status}"
                             + (f", final=0x{c4_final:08x}" if c4_final is not None else ""))
        non_phantom = any(b[3] != 0xff for b in decoded)
        if decoded and not any_timeout and non_phantom:
            return "NH9_FAST_READ_OK", reasoning + [
                "  → FAST_READ regen WORKS.  Real bytes (≠ 0xff phantom) read",
                "    via opcode 0x0B + stage3 do_wait_dummyclk FSM-unlock.",
                "    epcs_fast_read() validated on silicon.  Phase 3 closure",
                "    achieved.  Next: wire production callers (sd_boot, etc)",
                "    to epcs_fast_read in place of the removed epcs_read.",
            ]
        if decoded and not any_timeout and not non_phantom:
            return "NH9_PHANTOM_FF", reasoning + [
                "  → FAST_READ pre-amble + DV asserted, but all bytes 0xff.",
                "    Either flash is genuinely 0xff at offset 0 (unlikely on a",
                "    populated ALINX EPCS), or the data path is reading the",
                "    pre-charged ASMI dataout register instead of real flash",
                "    output.  Check Pin Info Table for DATA0 input direction.",
            ]
        if any_timeout:
            return "NH9_FAST_READ_HANG", reasoning + [
                "  → FAST_READ also hangs.  port_fast_read=PORT_USED regen did",
                "    not break the deadlock.  Likely stage4 / stage_cntr issue",
                "    deeper than the stage3 do_wait_dummyclk unlock can rescue.",
                "    Fall back to Plan B: hand-write SPI master directly against",
                "    cycloneive_asmiblock primitive (~200-400 LE, 2-3 h).",
            ]

    if seen_ill:
        return "NH5", reasoning + [
            "  → opcode rejected by flash (illegal_erase or illegal_write sticky).",
            "    Verify EPCS chip part number on AX301 silk-screen vs EPCS_TYPE."
        ]

    if a2_status == "ABSENT":
        return "NO_DIAG_ENTRY", reasoning + ["  → diagnostic never reached Phase A."]

    if a2_status == "TIMEOUT":
        # RDSR busy stuck — failure is at the megafunction-to-AS-pin layer.
        return "NH2_OR_NH3", reasoning + [
            "  → RDSR busy never fell.  Megafunction-to-AS-pin layer broken.",
            "    Likely NH2 (rublock-vs-asmiblock arch contention) or",
            "    NH3 (JTAG-volatile leaves pin mux / config controller",
            "    state different from EPCS cold-boot).  Cold-boot from",
            "    EPCS would discriminate (overwrites factory ALINX)."
        ]

    # NH8 v2 signature: B1 STATUS=0x00 (no busy ever rose) + B2 OK at i=0
    # + B4 TIMEOUT with STATUS=0x00.  Means: wrapper had rden=0 always,
    # so end_read_reg fired immediately on do_read entering, FSM aborted
    # before any byte shifted in.  See docs/captures/diag_nh8v2_rden0_fsm_abort.log
    # (md5 cd860075).
    if (b1_status == 0
            and b2_status == "OK" and b2_final == 0
            and b4_status == "TIMEOUT" and b4_final == 0):
        return "NH8_RDEN0_ABORT", reasoning + [
            "  → FSM aborted before any data shift (rden=0 too aggressive).",
            "    end_read_reg fires immediately on do_read because (~rden)=1",
            "    short-circuits the gate.  No bytes produced.  Wrapper must",
            "    hold rden=1 during shift and drop=0 only after last byte.",
            "    Matches signature of patch 0013 NH8 v2 (am_rden=1'b0)."
        ]

    # B2 TIMEOUT precedes B5 — pre-amble hanging is the operative failure
    # even if B5 happens to see a (likely phantom) DATAOUT byte because
    # B4 dv_wait short-circuits on a stale STATUS_DV bit.
    if b2_status == "TIMEOUT":
        # NH8 v3 trace has B5b/B5c — if they show the rden_drop pulse didn't
        # break the deadlock, surface that explicitly.
        if (b5c_status == "TIMEOUT" and b5c_final is not None
                and (b5c_final & 0x1)):
            return "NH8V3_RDEN_PULSE_NOOP", reasoning + [
                "  → Stage4 deadlock confirmed: even firmware-pulsed",
                "    CTRL.b7=ctrl_rden_drop has no effect.  B5c busy still",
                "    high after the pulse.  Root cause is structural: by",
                "    the time firmware can pulse, stage_cntr has drifted",
                "    off 2'b10 and FSM clk_en is frozen on ~end_read.",
                "    Wrapper-only fixes cannot break this — needs megafunction",
                "    regen (port_fast_read=PORT_USED) or hand-written SPI",
                "    master against cycloneive_asmiblock primitive.",
                "    Matches docs/captures/diag_nh8v3_rden_pulse_noop.log",
                "    (md5 b33a3230).",
            ]
        v = "STAGE4_DEADLOCK"
        notes = [
            "  → READ pre-amble busy never fell despite RDSR working.",
            "    This is the qmegawiz altasmi_parallel structural deadlock",
            "    (refuted hypothesis chain: NH3 destruct-cold-boot didn't",
            "    change it, NH5 mostly out since RDSR uses 0x05 fine, NH6/7",
            "    wrapper POR + read_address didn't help).  Real mechanism:",
            "    epcs_ctrl.v line 377 FSM clk_en gates on ~end_read; line",
            "    813 end_read needs rden=0 AND stage_cntr=2'b10 aligned.",
            "    Matches docs/captures/diag_baseline_hang.log (md5 d9330cae).",
        ]
        if b2_final is not None and (b2_final & 0x2):
            notes.append(
                "    Sticky STATUS_DV observed: am_data_valid level-locked"
                "  from megafunction's dvalid_reg per epcs_ctrl.v line 745"
                "  (only cleared by end_op_wire → end_read → can't fire here)."
            )
        if b5 is not None:
            notes.append(
                f"    B5 DATAOUT=0x{int(b5.group(1), 16):08x} is suspect —"
                " B4 short-circuited on stale DV bit from B1 (no real shift)."
            )
        return v, reasoning + notes

    if b5 is not None:
        return "SUCCESS_UNEXPECTED", reasoning + [
            "  → diagnostic READ succeeded.  The production epcs.c path",
            "    must have a bug this inline diagnostic happens to dodge.",
            "    Re-audit epcs.c vs the inline code in patch 0010."
        ]

    if b4_status == "TIMEOUT":
        if b4_final == 0:
            return "NH4_OR_NH3", reasoning + [
                "  → shift_bytes pulse did not land (busy clear, no DV).",
                "    Likely NH4 (1-cycle pulse too narrow for altasmi",
                "    internal FSM sync flops) or NH3 variant on the",
                "    data-shift path."
            ]
        if b4_final == 1:
            return "NH2_OR_NH3_READ", reasoning + [
                "  → busy stuck high during data shift.  NH2/NH3 on the",
                "    READ data path; flash never finishes asserting DV."
            ]
        return "ANOMALOUS_DV_WAIT", reasoning + [
            f"  → dv_wait timed out with unusual STATUS=0x{b4_final:08x}."
        ]

    return "INCONCLUSIVE", reasoning


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/tmp/diag_mode_p.log")
    ap.add_argument("--boot-timeout", type=float, default=15.0)
    ap.add_argument("--probe-timeout", type=float, default=20.0)
    ap.add_argument("--no-flash", action="store_true",
                    help="skip openFPGALoader; assume FPGA already running")
    args = ap.parse_args()

    preflight()
    log_path = Path(args.log)
    if log_path.exists():
        log_path.unlink()

    listener = Listener(DEV, BAUD, log_path)
    print("\n=== Listener-first ===", flush=True)
    listener.open_port()
    listener.start()
    time.sleep(0.3)
    print(f"  capturing to {log_path}", flush=True)

    if not args.no_flash:
        print("\n=== Flash ===", flush=True)
        cmd = [OFL_LOADER, "-c", "usb-blaster", "-m", BITSTREAM]
        print(f"  $ {' '.join(cmd)}", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f"  [FAIL] openFPGALoader rc={r.returncode}", flush=True)
            print(f"  stderr: {r.stderr[-500:]}", flush=True)
            listener.stop(); listener.join(timeout=1.0); listener.close()
            sys.exit(2)
        print("  flash OK", flush=True)
    else:
        print("\n=== Skipping flash (--no-flash) ===", flush=True)

    print("\n=== Wait for stage2 boot ===", flush=True)
    if not listener.wait_for(BANNER_MARK, args.boot_timeout):
        print(f"  [FAIL] no stage2 banner within {args.boot_timeout}s", flush=True)
        listener.stop(); listener.join(timeout=1.0); listener.close()
        sys.exit(3)
    print("  banner seen", flush=True)

    if not listener.wait_for(CRTM_LOCKED, args.boot_timeout):
        print(f"  [FAIL] CRTM locked marker not seen within {args.boot_timeout}s",
              flush=True)
        listener.stop(); listener.join(timeout=1.0); listener.close()
        sys.exit(4)
    print("  CRTM locked → dispatcher ready", flush=True)

    print("\n=== Send 'P' ===", flush=True)
    time.sleep(0.2)
    listener.send(b"P")
    print("  'P' sent", flush=True)

    print("\n=== Wait for diagnostic completion ===", flush=True)
    saw_entry = listener.wait_for(DIAG_ENTRY, 3.0)
    if not saw_entry:
        print("  [FAIL] [P] diagnostic entry banner not seen", flush=True)
        listener.stop(); listener.join(timeout=1.0); listener.close()
        sys.exit(5)
    print("  diagnostic entry seen", flush=True)

    saw_done = listener.wait_for(DIAG_DONE, args.probe_timeout)
    # Give a small tail window for any in-flight bytes after DONE.
    time.sleep(0.5)
    listener.stop()
    listener.join(timeout=1.0)
    listener.close()

    final = listener.snapshot()

    print("\n=== Verdict ===", flush=True)
    print(f"  diagnostic DONE: {'YES' if saw_done else 'NO (timeout)'}", flush=True)
    print(f"  log saved at   : {log_path} ({log_path.stat().st_size} B)", flush=True)

    verdict, reasoning = classify(final)
    print(f"  classification : {verdict}", flush=True)
    for line in reasoning:
        print(line, flush=True)

    if verdict == "SUCCESS_UNEXPECTED":
        sys.exit(0)
    if verdict in ("NO_DIAG_ENTRY", "INCONCLUSIVE"):
        sys.exit(6)
    sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        sys.exit(130)
