# Current-policy indicator-stop diagnostic

Current -3,500 TWD disaster stop and MFE_V1 are retained. Only the causal early-stop overlay changes.

| Variant | Scored | Net | PF | Win rate | Max DD | Indicator exits | Recovered later | Disaster stops |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| CURRENT_NEG_1R | 17 | -1778.00 | 0.93 | 0.35 | 9664.00 | 0 | 0 | 5 |
| FLOW_2OF3_2X_AFTER_NEG_0_25R | 18 | -2738.00 | 0.88 | 0.28 | 9816.00 | 12 | 4 | 1 |
| FLOW_2OF3_2X_AFTER_NEG_0_25R_VWAP | 17 | -6667.00 | 0.76 | 0.29 | 13745.00 | 4 | 2 | 3 |
| FLOW_2OF3_2X_AFTER_NEG_0_5R | 17 | -5616.00 | 0.79 | 0.29 | 12495.00 | 9 | 2 | 1 |

## Matched cohort

All variants are fully scored for the same 17 signals.

| Variant | Net | PF | Win rate | Max DD |
|---|---:|---:|---:|---:|
| CURRENT_NEG_1R | -1778.00 | 0.93 | 0.35 | 9664.00 |
| FLOW_2OF3_2X_AFTER_NEG_0_25R | -1820.00 | 0.92 | 0.29 | 8898.00 |
| FLOW_2OF3_2X_AFTER_NEG_0_25R_VWAP | -6667.00 | 0.76 | 0.29 | 13745.00 |
| FLOW_2OF3_2X_AFTER_NEG_0_5R | -5616.00 | 0.79 | 0.29 | 12495.00 |

## Conclusion

No tested overlay improved matched-cohort net PnL or Profit Factor. The -0.25R flow rule changed net PnL by -42.00 TWD and maximum drawdown by -766.00 TWD versus current policy.
Direction split is decisive: matched LONG changed from -2589.00 to -4983.00 TWD, while theoretical SHORT changed from 811.00 to 3163.00 TWD. Current production is long-only and historical short eligibility was not captured, so the short improvement cannot justify promotion.
The current live exit policy remains unchanged.

Three quality-limited sessions; overlapping independent signals are not one executable portfolio.
Research only: no broker connection, order, fill, or live behavior change.
