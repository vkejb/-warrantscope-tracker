# Near-miss exit validation

Every recorded rejected long candidate is entered at its causal suggested price. Entry gates remain unchanged in production; only exit variants are compared.

## Counts

- All near-miss events: 20
- First event per stock/day: 11
- First event per day: 3
- Near-misses duplicating an existing base entry: 2
- Expanded unique base plus near-miss signals: 28

## All near-miss events (independent, overlapping)

| Variant | Trades | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_BASELINE | 20 | -40125 | +0 | 2/18 | 0.258345 | -3006 | 40672 | 0 |
| EARLY_FAILURE_30S | 20 | -29253 | +10872 | 0/20 | 0.0 | -1463 | 29253 | 2 |
| EARLY_FAILURE_60S | 20 | -23363 | +16762 | 1/19 | 0.151639 | -1449 | 25906 | 1 |
| EARLY_FAILURE_90S | 20 | -21717 | +18408 | 2/18 | 0.391578 | -1983 | 24260 | 0 |
| EARLY_FAILURE_120S | 20 | -23913 | +16212 | 2/18 | 0.368884 | -2105 | 25857 | 0 |
| EARLY_FAILURE_150S | 20 | -25408 | +14717 | 2/18 | 0.354881 | -2188 | 27152 | 0 |
| EARLY_FAILURE_180S | 20 | -25057 | +15068 | 2/18 | 0.358072 | -2169 | 26801 | 0 |
| EARLY_FAILURE_240S | 20 | -26106 | +14019 | 2/18 | 0.348701 | -2227 | 28050 | 0 |
| EARLY_FAILURE_300S | 20 | -25958 | +14167 | 2/18 | 0.349994 | -2219 | 28301 | 0 |

## First near-miss per stock/day

| Variant | Trades | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_BASELINE | 11 | -13303 | +0 | 2/9 | 0.512353 | -3031 | 22150 | 0 |
| EARLY_FAILURE_30S | 11 | -16996 | -3693 | 0/11 | 0.0 | -1545 | 16996 | 2 |
| EARLY_FAILURE_60S | 11 | -10110 | +3193 | 1/10 | 0.292314 | -1429 | 14286 | 1 |
| EARLY_FAILURE_90S | 11 | -4823 | +8480 | 2/9 | 0.743457 | -2089 | 15665 | 0 |
| EARLY_FAILURE_120S | 11 | -5822 | +7481 | 2/9 | 0.705945 | -2200 | 16664 | 0 |
| EARLY_FAILURE_150S | 11 | -8314 | +4989 | 2/9 | 0.627024 | -2477 | 19156 | 0 |
| EARLY_FAILURE_180S | 11 | -8114 | +5189 | 2/9 | 0.632701 | -2455 | 19156 | 0 |
| EARLY_FAILURE_240S | 11 | -8513 | +4790 | 2/9 | 0.621476 | -2499 | 19156 | 0 |
| EARLY_FAILURE_300S | 11 | -9112 | +4191 | 2/9 | 0.605353 | -2565 | 19156 | 0 |

## First near-miss per day

| Variant | Trades | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_BASELINE | 3 | -2901 | +0 | 1/2 | 0.590081 | -3538 | 7077 | 0 |
| EARLY_FAILURE_30S | 3 | -6693 | -3792 | 0/3 | 0.0 | -2231 | 6693 | 1 |
| EARLY_FAILURE_60S | 3 | -407 | +2494 | 1/2 | 0.911194 | -2292 | 4583 | 0 |
| EARLY_FAILURE_90S | 3 | -1405 | +1496 | 1/2 | 0.748253 | -2790 | 5581 | 0 |
| EARLY_FAILURE_120S | 3 | -1904 | +997 | 1/2 | 0.686842 | -3040 | 6080 | 0 |
| EARLY_FAILURE_150S | 3 | -2901 | +0 | 1/2 | 0.590081 | -3538 | 7077 | 0 |
| EARLY_FAILURE_180S | 3 | -2901 | +0 | 1/2 | 0.590081 | -3538 | 7077 | 0 |
| EARLY_FAILURE_240S | 3 | -2901 | +0 | 1/2 | 0.590081 | -3538 | 7077 | 0 |
| EARLY_FAILURE_300S | 3 | -2901 | +0 | 1/2 | 0.590081 | -3538 | 7077 | 0 |

## Expanded unique base plus near-miss signals

| Variant | Trades | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_BASELINE | 28 | -50385 | +0 | 3/25 | 0.296662 | -2865 | 54523 | 0 |
| EARLY_FAILURE_30S | 28 | -43399 | +6986 | 0/28 | 0.0 | -1550 | 43399 | 3 |
| EARLY_FAILURE_60S | 28 | -37810 | +12575 | 1/27 | 0.099462 | -1555 | 40353 | 2 |
| EARLY_FAILURE_90S | 28 | -36862 | +13523 | 2/26 | 0.274927 | -1955 | 39405 | 1 |
| EARLY_FAILURE_120S | 28 | -29980 | +20405 | 3/25 | 0.414819 | -2049 | 37710 | 0 |
| EARLY_FAILURE_150S | 28 | -32973 | +17412 | 3/25 | 0.391923 | -2169 | 40303 | 0 |
| EARLY_FAILURE_180S | 28 | -33421 | +16964 | 3/25 | 0.388711 | -2187 | 40751 | 0 |
| EARLY_FAILURE_240S | 28 | -34967 | +15418 | 3/25 | 0.378022 | -2249 | 42098 | 0 |
| EARLY_FAILURE_300S | 28 | -36914 | +13471 | 3/25 | 0.365368 | -2327 | 44245 | 0 |

## Results by rejected gate

| Rejected by | Events | Baseline | Least-negative variant | Result |
|---|---:|---:|---|---:|
| ANTI_CHASE_OPENING_EXTENSION | 10 | -17178 | EARLY_FAILURE_60S | -7599 |
| ANTI_CHASE_VWAP_EXTENSION | 1 | -3534 | CURRENT_BASELINE | -3534 |
| CONFIRMATIONS_INCOMPLETE | 2 | -7492 | EARLY_FAILURE_60S | -3003 |
| RELATIVE_STRENGTH_BELOW_THRESHOLD | 6 | -8292 | EARLY_FAILURE_90S | 2533 |
| STOCK_5M_HISTORY_MISSING | 1 | -3629 | EARLY_FAILURE_30S | -1235 |

## Interpretation

- All 20 near-misses remain negative under every exit. The least-negative checkpoint is EARLY_FAILURE_90S at -21717 TWD.
- First-per-day is least negative under EARLY_FAILURE_60S at -407 TWD, but this is only three observations.
- After removing duplicate entries, the expanded 28-signal diagnostic is least negative under EARLY_FAILURE_120S at -29980 TWD.
- The apparently positive relative-strength-rejected subgroup is driven by the 2026-09-30 1709 trade (+9,801 TWD under baseline). Removing that one winner makes its 90-second result negative again (-7,268 TWD).
- Exit timing reduces losses but does not rescue the rejected-entry population. This supports keeping entry quality gates while continuing exit shadow tests.
- No result is suitable for production promotion from this sample.

## Limitations

- Near-misses were rejected by production entry gates; forcing them in is counterfactual.
- All-event and per-symbol totals contain overlapping positions and are not feasible account PnL.
- First-per-day is mechanically feasible but has only three sessions.
- The 20260930 session is stitched and has four untimestamped callback errors.
- Only the three recent sessions have synchronized permanent 0050 context.

No production or live behavior changed.
