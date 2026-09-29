# Joint intraday strategy diagnostic

All variants are backtest-only. Delayed variants use a new causal fill after the confirmation checkpoint; they never retain the original entry price.

| Variant | Entered | Anti-chase skip | Confirmation reject | Unscorable | W/L | Win rate | Net PnL | Delta | PF | Avg loser | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BASELINE | 17 | 0 | 0 | 0 | 6/11 | 35.3% | -1,778 | 0 | 0.93 | -2,371 | 9,664 |
| ANTI_CHASE_ONLY | 8 | 9 | 0 | 0 | 6/2 | 75.0% | 20,029 | 21,807 | 5.69 | -2,134 | 3,684 |
| ANTI_CHASE_WITH_EXIT_PROTECTION | 8 | 9 | 0 | 0 | 6/2 | 75.0% | 20,527 | 22,305 | 6.44 | -1,886 | 3,684 |
| ANTI_CHASE_CONFIRM_60S | 3 | 9 | 5 | 0 | 3/0 | 100.0% | 13,857 | 15,635 | N/A | N/A | 0 |
| JOINT_WITH_EXIT_PROTECTION | 3 | 9 | 5 | 0 | 3/0 | 100.0% | 13,857 | 15,635 | N/A | N/A | 0 |

This comparison cannot authorize a live change: the sample is small, all source sessions are partial, and one session has callback errors.
