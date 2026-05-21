# H1/H2 sweep capture — `<DATE>` `<BUILD-LABEL>`

Build label: `baseline | mask_FFFF | mask_6996`
Flashed RBF: `h1h2_top.rbf | h1h2_mask_FFFF.rbf | h1h2_mask_6996.rbf`
EPCS pre-flash dump md5: `<from openFPGALoader --dump-flash>`

## Observation method

RTL sweeps `sweep[3:0]` once per ~1.34 s tick (50 MHz / 2^26). On each tick:

- **LED0** = `combout` (LUT output for the current `sweep[3:0]`)
- **LED1..LED3** = `sweep[1..3]`
- **`sweep[0]`** = inferred from LED[3:1] transitions: when LED[3:1] does
  NOT change between two consecutive ticks, the earlier tick has
  `sweep[0]=0` and the later has `sweep[0]=1`. (Equivalently: any tick
  immediately after a LED[3:1] change has `sweep[0]=0`.)

There is no KEY2 reset (E16 doesn't support weak pull-up on Cyclone IV E,
see [[reference-ax301-board-quirks]]). After power-on, sweep starts at 0
and rolls 0..15..0..15 forever; the observer logs two full cycles
(~42 s) and uses LED[3:1] transitions to anchor `sweep[0]` parity.

Wall-clock between ticks is ~1.34 s, full 16-step sweep takes ~21 s.
Running two consecutive sweeps cross-checks for glitches: the second
sweep must reproduce the first row-for-row.

## Sweep log

| tick | LED3 | LED2 | LED1 | LED0 | sweep[3:0] (= {LED3,LED2,LED1, tick&1}) | expected TT[sweep] | match |
|-----:|:----:|:----:|:----:|:----:|:--------------------------------------:|:------------------:|:-----:|
| 0    |      |      |      |      | 0000                                   |                    |       |
| 1    |      |      |      |      | 0001                                   |                    |       |
| 2    |      |      |      |      | 0010                                   |                    |       |
| 3    |      |      |      |      | 0011                                   |                    |       |
| 4    |      |      |      |      | 0100                                   |                    |       |
| 5    |      |      |      |      | 0101                                   |                    |       |
| 6    |      |      |      |      | 0110                                   |                    |       |
| 7    |      |      |      |      | 0111                                   |                    |       |
| 8    |      |      |      |      | 1000                                   |                    |       |
| 9    |      |      |      |      | 1001                                   |                    |       |
| 10   |      |      |      |      | 1010                                   |                    |       |
| 11   |      |      |      |      | 1011                                   |                    |       |
| 12   |      |      |      |      | 1100                                   |                    |       |
| 13   |      |      |      |      | 1101                                   |                    |       |
| 14   |      |      |      |      | 1110                                   |                    |       |
| 15   |      |      |      |      | 1111                                   |                    |       |

LED notation: `on` / `off`.

Second sweep (sanity) — must match first row-for-row, else timing/glitch suspected:

| tick | LED0 | matches first sweep? |
|-----:|:----:|:--------------------:|
| 0    |      |                      |
| ...  |      |                      |
| 15   |      |                      |

## Decoded truth table

Reconstruct bit `i` of the observed 16-bit TT from LED0 at `sweep[3:0] = i`:

```
observed TT = 0b<bit15><bit14>...<bit1><bit0> = 0x<hex>
expected TT = 0x<expected>
verdict     = match | mismatch | partial (permutation)
```

## Pin-friendly LE location verified

From `quartus/output_files/h1h2_top.fit.rpt`:

```
<paste the line(s) showing sentinel_lut placed at LCCOMB_X10_Y2_N0>
```

## H1/H2 decision (filled in after step F)

Cross-reference outcome table in `docs/notes/h1h2_silicon_test_2026-05-21.md`:

- Step D (mask=FFFF) result: `<LED always on | LED on/off mixed | LED always off>`
- Step F (mask=6996) result: `<TT=0x6996 | TT=permutation | TT≠0x6996 | LED always off>`
- Verdict: `<H1 wins | H1 with input renaming | H2 confirmed | codec/CRC bug>`
- mode H plan action: `<as written | sentinel doc note + proceed | shrink scope or extend codec | block + debug>`
