# Entry confirmation sensitivity study

All variants keep the production stock universe, base signal, sizing, exits and cost model.

| Variant | Trades | W/L | Win rate | Net PnL | Avg/trade | PF | vs baseline | Robust? |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| BASELINE | 1 | 0/1 | 0.0% | -3579 | -3579 | 0.0 | +0 | - |
| ROLLING_90S_TWO_HITS | 1 | 0/1 | 0.0% | -3579 | -3579 | 0.0 | +0 | - |
| STRONG_ONESHOT_BASE_CHASE | 2 | 0/2 | 0.0% | -7122 | -3561 | 0.0 | -3543 | - |
| STRONG_ONESHOT_RELAXED_CHASE | 3 | 1/2 | 33.3% | -2946 | -982 | 0.586352 | +633 | FRAGILE |
| STRONG_ONESHOT_RELAXED_RS_2PCT | 2 | 1/1 | 50.0% | 597 | 298 | 1.166806 | +4176 | FRAGILE |
| HOLD_30S_BASE_CHASE | 1 | 0/1 | 0.0% | -3579 | -3579 | 0.0 | +0 | - |
| HOLD_60S_BASE_CHASE | 1 | 0/1 | 0.0% | -3579 | -3579 | 0.0 | +0 | - |
| HOLD_30S_RELAXED_CHASE | 2 | 1/1 | 50.0% | -1201 | -600 | 0.664431 | +2378 | FRAGILE |
| HOLD_60S_RELAXED_CHASE | 1 | 0/1 | 0.0% | -3579 | -3579 | 0.0 | +0 | - |

## Per-session result

| Variant | Date | Trade | Entry | Exit | Reason | Net PnL |
|---|---|---|---:|---:|---|---:|
| BASELINE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| BASELINE | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| BASELINE | 20261001 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| ROLLING_90S_TWO_HITS | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| ROLLING_90S_TWO_HITS | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| ROLLING_90S_TWO_HITS | 20261001 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| STRONG_ONESHOT_BASE_CHASE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| STRONG_ONESHOT_BASE_CHASE | 20260930 | 3016 嘉晶 | 171.00 | 168.00 | STOP_LOSS | -3543 |
| STRONG_ONESHOT_BASE_CHASE | 20261001 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| STRONG_ONESHOT_RELAXED_CHASE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| STRONG_ONESHOT_RELAXED_CHASE | 20260930 | 3016 嘉晶 | 171.00 | 168.00 | STOP_LOSS | -3543 |
| STRONG_ONESHOT_RELAXED_CHASE | 20261001 | 3094 聯傑 | 64.20 | 66.50 | MFE_PROFIT_PROTECTION | 4176 |
| STRONG_ONESHOT_RELAXED_RS_2PCT | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| STRONG_ONESHOT_RELAXED_RS_2PCT | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| STRONG_ONESHOT_RELAXED_RS_2PCT | 20261001 | 3094 聯傑 | 64.20 | 66.50 | MFE_PROFIT_PROTECTION | 4176 |
| HOLD_30S_BASE_CHASE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| HOLD_30S_BASE_CHASE | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| HOLD_30S_BASE_CHASE | 20261001 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| HOLD_60S_BASE_CHASE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| HOLD_60S_BASE_CHASE | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| HOLD_60S_BASE_CHASE | 20261001 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| HOLD_30S_RELAXED_CHASE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| HOLD_30S_RELAXED_CHASE | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| HOLD_30S_RELAXED_CHASE | 20261001 | 3094 聯傑 | 64.40 | 65.80 | MFE_PROFIT_PROTECTION | 2378 |
| HOLD_60S_RELAXED_CHASE | 20260929 | 3605 宏致 | 182.00 | 179.00 | STOP_LOSS | -3579 |
| HOLD_60S_RELAXED_CHASE | 20260930 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |
| HOLD_60S_RELAXED_CHASE | 20261001 | No trade | - | - | NO_APPROVED_LONG_ENTRY | 0 |

## Interpretation

- Variants improving aggregate net PnL: STRONG_ONESHOT_RELAXED_CHASE, STRONG_ONESHOT_RELAXED_RS_2PCT, HOLD_30S_RELAXED_CHASE.
- Every improving variant is fragile if its best single day is removed.
- Any apparent improvement must be checked per day; three sessions cannot establish a production threshold.
- The RS 2% version is explicitly diagnostic and must not be treated as fitted production logic.

## Limitations

- Only three synchronized-0050 sessions are available; 20260930 is a diagnostic stitch with four untimestamped callback errors.
- Delayed hold checks require price above breakout/VWAP, non-negative post-signal large-trade flow, non-negative fresh book imbalance, and a fresh 0050 relative-strength recheck.
- No production or broker behavior was changed.
