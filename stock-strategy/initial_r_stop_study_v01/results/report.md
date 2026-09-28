# Initial R stop diagnostic

1R is fixed at net -3,500 TWD. Entry signals, sizing, fees, tax, slippage and non-stop exits are unchanged.

| Variant | Scored | Net | PF | Win rate | Avg loss | Max DD | Stops | Recovered later | Avg hold sec |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_NEG_1R | 17 | -1778.00 | 0.93 | 0.35 | -2370.55 | 9664.00 | 5 | 0 | 2845.14 |
| STOP_NEG_0_5R | 18 | -27378.00 | 0.14 | 0.06 | -1871.76 | 31820.00 | 16 | 9 | 377.44 |
| STOP_0R | 18 | -25981.00 | 0.00 | 0.00 | -1443.39 | 25981.00 | 18 | 11 | 1.25 |

## Matched cohort

All variants are fully scored for the same 17 signals.

| Variant | Net | PF | Win rate | Max DD |
|---|---:|---:|---:|---:|
| CURRENT_NEG_1R | -1778.00 | 0.93 | 0.35 | 9664.00 |
| STOP_NEG_0_5R | -25463.00 | 0.15 | 0.06 | 29905.00 |
| STOP_0R | -24066.00 | 0.00 | 0.00 | 24066.00 |

## Conclusion

The -0.5R stop is not supported: it produced 16 stop exits and 9 of them later recovered to positive PnL on the recorded path. Its smaller average loss did not offset the winners it cut.
The 0R rule exited all 18 trades, averaged 1.25 seconds of holding time, and remained materially negative because spread, fees, tax and slippage make immediate round-trip PnL negative.
The current -1R / net -3,500 TWD boundary remains the least-bad tested fixed initial stop; this sample does not justify changing live behavior.

Three quality-limited sessions; overlapping independent signals are not one executable portfolio.
Research only: no broker connection, order, fill, or live behavior change.
