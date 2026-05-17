# Phase 3 mode 'P' diagnostic captures

Permanent record of UART captures from the Phase 3 mode 'P' hang
investigation, 2026-05-17 evening session.  Each file is the raw
binary captured by the listener wrapper (`scripts/diag_mode_p_test.py`
or `scripts/nh1_mode_p_test.py`).  md5 + size kept stable here so
future sessions can byte-compare new traces against these reference
signatures.

| File | Size | md5 | Bitstream | What it shows |
|------|------|-----|-----------|---------------|
| `prod_modeP_hang.log` | 340 B | `d8dbe639bb7ae183c508cdb73a6ebddf` | NH1 `871f1ecc…` (and identical pre-NH1 `fcfd8122…`) | Production `epcs_read()` mode 'P' hang. Final UART line is `[stage2] Mode: EPCS probe — read 64 B @0x000000\r\n` then silence. Hang is in `wait_data_valid()` from epcs.c. 3/3 byte-identical across NH1 + pre-NH1 bitstreams. |
| `diag_baseline_hang.log` | 1510 B | `d9330cae7e1effbc39447fc1345ad39a` | Diagnostic `f931e805…`, NH6 `aa41f714…`, NH7 `011a4c37…`, NH8 v1 `bdee9496…` (all four byte-identical) | Diagnostic mode 'P' with patch 0010 inline STATUS-trace. Phase A RDSR clean (6 iters, ESTAT=0x00); Phase B CTRL=READ → STATUS=0x03 (busy=1, DV=1) immediately, busy never clears across 10M poll iters. The canonical "wrapper-rden=1 stage4 deadlock" signature. |
| `diag_nh8v2_rden0_fsm_abort.log` | 1413 B | `cd860075d6a24a3b1fba0f9ef1961487` | NH8 v2 `891efece…` (am_rden = 1'b0 constant) | Different failure mode: B1 STATUS=0x00 IMMEDIATELY after CTRL=READ (busy never even asserts), B2 OK at i=0, B4 dv_wait TIMEOUT with STATUS=0x00. The FSM aborts before any byte shifts because `end_read` fires immediately on `do_read` (no rden gate). Confirms rden controls end-of-read mechanism but bare `rden=0` is too aggressive. |
| `diag_nh8v3_rden_pulse_noop.log` | 1630 B | `b33a323081b77cc6c07c96f3d0f8e44a` | NH8 v3 `1787cd8a…` (am_rden = ~ctrl_rden_drop, firmware-pulsed via CTRL.b7) | Same body as `diag_baseline_hang.log` for B1-B5 (busy stuck at STATUS=0x03), PLUS new B5b/B5c lines showing the firmware-issued rden_drop pulse has no effect: B5b STATUS=0x03, B5c post-pulse wait TIMEOUT. Confirms 1-cycle rden=0 pulse cannot escape the stage4 deadlock because `stage_cntr` has already drifted off 2'b10 by the time firmware can pulse. |

## Comparison points

- Production `wait_data_valid()` hang vs diagnostic `wait_not_busy()`
  pre-amble hang: same root cause (stage4 deadlock), different
  apparent hang location because production code never gets past
  pre-amble wait to reach the data_valid wait.
- 4-way byte-identical (`d9330cae…`) tells us: wrapper RTL changes
  to POR reset (NH6), read_address connection (NH7), or 1-cycle
  rden ack (NH8 v1) do NOT change the megafunction's runtime
  behavior — the deadlock is structural in the qmegawiz output.
- `cd860075…` (rden=0) vs `d9330cae…` (rden=1) gives the single
  point of empirical confirmation that rden DOES control end_read
  semantics in this megafunction.  But neither polarity is
  workable on its own.

## How to reproduce

```sh
make unapply-patches && make apply-patches
make rot-bitstream
python3 scripts/diag_mode_p_test.py --log /tmp/new_run.log
md5sum /tmp/new_run.log
```

If the md5 matches `diag_baseline_hang.log`, the current bitstream
is in the same broken-FSM state as our reference (no fix yet).  If
it differs, the FSM behavior changed — diff against the reference
to see how.

## Cleanup criterion

Delete this directory only after Phase 3 closes (mode 'P' actually
returns real flash content), because these captures are the
empirical record that future qmegawiz regens (or hand-written SPI
master) must beat.
