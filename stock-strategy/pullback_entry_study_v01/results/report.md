# Causal pullback-entry diagnostic

Same frozen signals, capital rule, fees, tax, slippage and current exit policy.
A relative extreme is confirmed only by later data; no future low/high is used.

| Variant | Signals | Entries | No entry | Unscorable | Net | PF | Win rate | Max DD | Stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| IMMEDIATE | 18 | 18 | 0 | 1 | -1778.00 | 0.93 | 0.35 | 9664.00 | 5 |
| PULLBACK_0_5R_60S | 18 | 7 | 11 | 0 | -14968.00 | 0.21 | 0.14 | 14968.00 | 5 |
| PULLBACK_1_0R_90S | 18 | 1 | 17 | 0 | -3588.00 | 0.00 | 0.00 | 3588.00 | 1 |

## Matched cohort

Only the same 1 fully scored signals are compared below.

| Variant | Net | PF | Win rate | Max DD |
|---|---:|---:|---:|---:|
| IMMEDIATE | -3593.00 | 0.00 | 0.00 | 3593.00 |
| PULLBACK_0_5R_60S | -3588.00 | 0.00 | 0.00 | 3588.00 |
| PULLBACK_1_0R_90S | -3588.00 | 0.00 | 0.00 | 3588.00 |

## Pairwise effect versus immediate entry

| Variant | Paired | Immediate net | Pullback net | Delta | Missed immediate net |
|---|---:|---:|---:|---:|---:|
| PULLBACK_0_5R_60S | 7 | -15477.00 | -14968.00 | 509.00 | 13699.00 |
| PULLBACK_1_0R_90S | 1 | -3593.00 | -3588.00 | 5.00 | 1815.00 |

## Conclusion

Neither pullback rule is supported for promotion. The 0.5R rule slightly improved entry timing on its seven paired trades, but those trades were predominantly weak and five still hit the fixed stop. It also skipped the immediate-entry cohort that contained most of the available profits. The 1R rule produced only one entry, which is not an evaluable sample.

Three quality-limited sessions; overlapping independent signals are not one executable portfolio.
Research only: no broker connection, order, fill, or live behavior change.