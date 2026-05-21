# docs/analysis/h1h2_silicon_test/

## Status: ABANDONED 2026-05-21 — see test plan doc

The H1/H2 silicon discriminator test designed in
`docs/notes/h1h2_silicon_test_2026-05-21.md` could not be executed in
our Quartus Lite 21.1 toolchain. The investigation produced one
durable finding (see [[feedback-quartus-lite-no-lccomb-lock]] in
memory): **Quartus 21.1.0 Lite Edition silently drops all
`set_location_assignment LCCOMB_X*_Y*_N*` assignments**, including
those in the provider's known-good `EP4CE6/jailbreak/jbscan` reference.

The test design relied on locking a sentinel LE at a specific
pin-friendly (X, Y, N); without that, the host-side σ⁻¹ codec cannot
target the right CRAM cells.

Per the late-evening 2026-05-21 strategy review the project went with
"Path B1": narrow mode H first-ship scope to symmetric masks
`{0x0000, 0xFFFF}` and defer M0-M4 silicon validation until either
Quartus Pro/Standard is available or a working post-fit placement-query
mechanism is found in Lite. See `~/.claude/plans/encapsulated-cooking-bunny.md`
(REVISED 2026-05-21).

## What's in this directory

```
h1h2_silicon_test/
├── README.md                      (this file)
├── host/                          ← survives; general-purpose codec validators
│   ├── build_lutcodec_so.sh       (build the shared library)
│   ├── lutcodec.so                (host ctypes wrapper around on-chip C codec)
│   ├── edit_rbf.py                (apply target LUT mask + CRC repair)
│   └── scan_rbf.py                (reverse-engineer LUT mask at any (x,y,n))
├── captures/                      ← empty; would have held silicon traces
│   └── sweep_template.md          (capture protocol design, never executed)
└── abandoned_2026-05-21/          ← dead-end RTL / QSF / fit artifacts
    ├── rtl/h1h2_top.v             (sentinel + 16-LUT chain — neither worked)
    └── quartus/                   (build database from the failed attempts)
```

## What survives and how to reuse it

The `host/` tools have no dependency on the abandoned RTL / QSF and
are silicon-tested codec validators. Reuse pattern:

```bash
# Build the library once
./host/build_lutcodec_so.sh

# Read the current LUT mask at any pin-friendly (x,y,n) in any .rbf
python3 host/scan_rbf.py path/to/any.rbf --probe 22,8,12

# Apply a target LUT mask to a baseline .rbf and patch frame CRCs
python3 host/edit_rbf.py baseline.rbf out.rbf --xyn 22,8,12 --mask 0x6996
```

These tools will ride along into a future Path D test (mode g writes
asymmetric mask at an existing production-iron LE) or any retry of the
M0-M4 chain on a different toolchain.

## What NOT to do

- Do not copy `abandoned_2026-05-21/rtl/h1h2_top.v` or `quartus/h1h2_top.qsf`
  into the production iron expecting the QSF's `LCCOMB_X10_Y2_N0` lock
  to do anything; it won't in Lite. Both files are kept only as the
  historical record of what was tried.
- Do not iterate on the lock pattern in the QSF (single LE, chain,
  pin-friendly Y, etc.) — all three failure modes are already verified
  empirical dead-ends in this session's TCL log.
