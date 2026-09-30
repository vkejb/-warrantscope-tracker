# 2026-09-30 near-miss entry counterfactual

Each row is an independent forced entry. Production entry gates were not changed.

| Time | Stock | Blocked by | Entry x qty | Exit | Reason | Net PnL | MFE | MAE |
|---|---|---|---:|---:|---|---:|---:|---:|
| 09:07:00 | 3016 嘉晶 | CONFIRMATIONS_INCOMPLETE | 171.00 x 1000 | 168.00 | STOP_LOSS | -3543 | -1049 | -3543 |
| 09:08:00 | 1709 和益 | RELATIVE_STRENGTH_BELOW_THRESHOLD | 44.70 x 4000 | 47.30 | MFE_PROFIT_PROTECTION | 9801 | 13392 | -2171 |
| 09:11:00 | 2033 佳大 | ANTI_CHASE_OPENING_EXTENSION | 40.35 x 4000 | 40.25 | MFE_PROFIT_PROTECTION | -918 | 3671 | -2315 |
| 09:13:30 | 2033 佳大 | ANTI_CHASE_OPENING_EXTENSION | 41.05 x 4000 | 40.25 | STOP_LOSS | -3721 | 868 | -3721 |
| 09:56:00 | 4956 光鋐 | ANTI_CHASE_OPENING_EXTENSION | 48.30 x 3000 | 48.25 | MFE_PROFIT_PROTECTION | -616 | 3275 | -915 |
| 09:56:30 | 4956 光鋐 | ANTI_CHASE_OPENING_EXTENSION | 48.70 x 3000 | 47.65 | STOP_LOSS | -3613 | 2074 | -3613 |
| 10:14:30 | 4956 光鋐 | RELATIVE_STRENGTH_BELOW_THRESHOLD | 47.85 x 3000 | 46.80 | STOP_LOSS | -3605 | -911 | -3605 |
| 10:30:30 | 6456 GIS-KY | ANTI_CHASE_OPENING_EXTENSION | 93.60 x 2000 | 92.10 | STOP_LOSS | -3596 | 1393 | -3596 |
| 10:48:30 | 1709 和益 | ANTI_CHASE_OPENING_EXTENSION | 47.45 x 4000 | 47.40 | MFE_PROFIT_PROTECTION | -811 | 2382 | -3006 |

## Summary

- Scorable: 9 / 9
- Winners / losers: 1 / 8
- Independent arithmetic sum (not executable as one day): NT$-10622.0
- Average per independent opportunity: NT$-1180.22
- First chronological opportunity only: 3016 / NT$-3543.0

## Limitations

- The nine entries are independent counterfactuals and cannot be summed as a feasible one-position trading day.
- The production strategy would accept at most one trade; first-chronological-only is the feasible mechanical override scenario.
- The early source has four aggregate callback errors without timestamps, so this remains diagnostic-only.
- No broker connection or order submission was performed.
