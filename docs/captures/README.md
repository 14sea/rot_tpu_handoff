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
| `diag_nh9_fast_read_ok.log` | 2487 B | `9d80d60cf30b447b0db03520f6d48572` | NH9 `5145a6d7…` (qmegawiz regen w/ port_fast_read=PORT_USED) | First silicon win: Phase C FAST_READ returns real EPCS bytes `0x7d 0x21 0x0f 0x00` (not phantom 0xff). Phase A RDSR OK i=2 (first-run-after-flash; later runs intermittent). C4 RDEN_DROP pulse TIMEOUT — termination still broken at this stage. Listener cleanup race truncated capture after C3 (fix landed in same session). Capture commit: `a951caa`. |
| `diag_nh10_auto_terminate_ok.log` | 2371 B | `88548dd4c01390bb6784250ae691cdc2` | NH10 `43f8578e…` (wrapper auto-terminate: `am_rden = ~ctrl_rden_drop & ~(am_busy & ~ctrl_fast_read)`) | Termination closure: C5 STATUS=0x02 (busy DROPPED, only DV bit left) vs prior NH9 0x03 stuck.  3/3 deterministic.  Phase C bytes `0x21 0x00 0x88 0x00` — different from NH9's `0x7d 0x21 0x0f 0x00` despite reading same flash address (cross-bitstream placement-dependent metastability hint; fully characterized later in Phase D probe). |
| `diag_nh10_phase_d_alignment_probe.log` | 3870 B | `8fba57aaabfd6f136c67550f2a887b19` | NH10 `bc9ba066…` (NH10 + Phase D diag firmware) | Phase 3 silicon-status reframe.  D0 addr=0x100000 → `ff ff ff ff` (wrapper byte alignment OK for blank flash).  D1 addr=0 same boot → `18 81 21 00` (≠ Phase C's `a1 00 d1 00`; megafunction `read_add_cntr` not reset between consecutive FAST_READs).  D2 addr=0x10 → `15 80 14 3f` (offset arithmetic OK).  Phase A 3/3 fresh-boot TIMEOUT (cold-boot intermittent RDSR).  3/3 deterministic across re-runs.  Root cause class: timing slack `-1.182 ns` setup CLOCK metastability + state pollution between ops. |
| `diag_phase4_pre_rtl_fix.log` | 457 B | `71cd4f0a4cad88535da14d91bada4a2a` | Firmware-only `f351496124bab1f2423c076896ad22a0` (Phase 4 firmware on Phase 2 wrapper, no RTL fix) | First Phase 4 silicon test.  Stage2 boots clean, dispatcher reaches "CRTM locked", new menu shows `r=reconfig`, 'r' command accepted: `[stage2] Mode: remote reconfig` + `[rot] altremote: pgm_sel=0x000000 ... triggering reconfig` — then silence.  rconfig never asserts on the rublock pin.  Hypothesis at this point: rconfig pulse width too narrow (NH11). |
| `diag_phase4_nh11_fsm_unreachable.log` | 457 B | `71cd4f0a4cad88535da14d91bada4a2a` | NH11 only `ea758429...` (16-cycle rconfig hold added; auto_reconfig latch NOT yet added) | **Byte-identical to pre-RTL-fix capture.**  NH11 widening alone changes ZERO bytes of UART → FSM never reaches S_RECONFIG in the first place, so hold width is moot.  Empirical falsification of the "rconfig pulse width" hypothesis as load-bearing.  Drove the NH12 audit which found the real bug: `S_LOAD_POST` reads `trig_reconfig\|trig_shift_in` ~31 cycles after the bus block cleared them. |
| `diag_phase4_nh12_noinput_stable.log` | 400 B | `e889e396c98f8b350f29b74e068f0122` | NH11+NH12 `60094abc6c87914c7f9cd72fa5c62adc` | Sanity capture: NH12 bitstream flashed, NO 'r' sent.  Output shows prior cold-boot factory ALINX timestamp `2010-3-17 14:30:46` (printed BEFORE openFPGALoader's reset took effect), then null-byte transition, then `[stage2] ready` + dispatcher + "CRTM locked".  Stage2 stable for 24 s of idle.  Refutes the "device reset on flash" alarm: the factory banner was an artifact of the JTAG-load handover, not NH12 misfiring on boot.  Also refutes the prior-session WD-suicide hypothesis on this hardware. |
| `diag_phase4_nh12_reconfig_ok.log` | 3357 B | `613db17a485ce370b030eac806121f14` | NH11+NH12 `60094abc6c87914c7f9cd72fa5c62adc` | **Phase 4 silicon closure capture.**  Stage2 → dispatcher → `[stage2] Mode: remote reconfig — boot from EPCS page 0 (factory ALINX)` → `[rot] altremote: pgm_sel=0x000000 WD_EN=0 AP_CONFIG_SEL=0 — triggering reconfig` → factory ALINX banner `2010-3-17 14:19:0` appears immediately, then runs its periodic timestamp loop (14:19:1 ... 14:19:17) for the remaining ~17 s.  Matches success criterion from the brief. |
| `diag_phase3_nh13_reset_stretcher_refuted.log` | 3549 B | `a843ddc36661befa82db745105e5659c` | NH13 `c58d52394a6b80fd14ef0f0d85ba8ff8` | NH13 (wrapper ctrl_reset 16-cycle stretcher + firmware DC_RESET pulse before each FAST_READ in mode_epcs_probe Phase C/D).  HW result: **REFUTED** as Phase 3 root cause.  Phase C addr=0 bytes `00 42 18 00` ≠ Phase D D1 addr=0 bytes `84 10 42 df` in same boot.  Cross-bitstream variance also persists.  Drove NH14 RADDR probe. |
| `diag_phase3_nh14_raddr_probe.log` | 3794 B | `646e17191b158f83f5db21b76c2bb6e0` | NH13+NH14 `5e9e0b2c372cb4715f95108089a7ebac` | **Essential Phase 3 diagnostic.**  Adds `*DP_RADDR` print at mode_epcs_probe entry + Phase C checkpoints + Phase D pre/post-ADDR-write.  Findings: (a) POR RADDR=0 (clean); (b) DC_RESET pulse DOES reset RADDR to 0 between ops (NH13 stretcher works); (c) **`*DP_ADDR` writes do NOT update RADDR** — the addr_reg → read_address linkage is broken at the megafunction interface; (d) Phase C 4-byte read advances RADDR by 0x81E85 = 532613 — megafunction streams flash continuously while rden=1, SHIFT_BYTES latches one byte at a non-deterministic stream position.  Conclusion: Phase 3 content-read accuracy is a wrapper SHIFT_BYTES ↔ data_valid handshake redesign + addr→pre-amble verification — Phase 9-class scope. |

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

**Phase 3 partial-closure update (2026-05-17 night)**: NH9 +
NH10 + Phase D establish (a) blank-flash detect is reliable
silicon-closed, (b) READ termination is silicon-closed via NH10
auto-terminate, but (c) content reads are unreliable per
consecutive read and per bitstream rebuild (read_add_cntr state
pollution + placement-dependent metastability from negative
timing slack).  Captures retained for the content-accuracy
follow-up work — when CTRL_RESET pulse / wrapper auto-reset /
SDC fix is attempted, the new run's bytes can be compared
byte-for-byte against these references to know if the fix
landed.

**Phase 4 closure (2026-05-17 night)**: NH11 (16-cycle rconfig
hold; defensive, not load-bearing) + NH12 (auto_reconfig latch;
load-bearing) close Phase 4 on silicon.  `diag_phase4_pre_rtl_fix`
and `diag_phase4_nh11_fsm_unreachable` are byte-identical, which
is the empirical refutation that drove the NH12 audit.
`diag_phase4_nh12_reconfig_ok` is the success capture — factory
ALINX banner appears immediately after the wrapper trigger.
Reference for any future Phase 4 wrapper rework: a new run's
post-trigger bytes must start with `2010-3-17 14:` within ~150 ms.
