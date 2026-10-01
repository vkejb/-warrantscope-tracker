# One-time recovery re-entry validation

A stopped trade may re-enter once only after a fresh causal recovery above the original net-PnL threshold, breakout boundary and session VWAP. Every re-entry uses a new adverse fill and a second fee/tax cycle.

## Base signals

| Rank | Variant | Net PnL | vs no re-entry | PF | Avg loser | Max DD | Reentries | Profitable | Recovered to profit | Winners harmed | LOTO robust |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_0S | -4163 | -2119 | 0.776411 | -2660 | 16219 | 2 | 1 | 0 | 0 | NO |
| 2 | ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_30S | -4671 | -2627 | 0.75579 | -2732 | 16727 | 2 | 1 | 0 | 0 | NO |
| 3 | ONE_REENTRY_RECOVERY_50PCT_R_CONFIRM_0S | -4672 | -2628 | 0.755751 | -2733 | 16728 | 2 | 1 | 0 | 0 | NO |
| 4 | ONE_REENTRY_RECOVERY_50PCT_R_CONFIRM_30S | -8171 | -6127 | 0.638883 | -3232 | 20227 | 2 | 0 | 0 | 0 | NO |
| 5 | ONE_REENTRY_RECOVERY_50PCT_R_CONFIRM_60S | -8677 | -6633 | 0.624908 | -3305 | 20733 | 2 | 0 | 0 | 0 | NO |
| 6 | ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_0S | -9155 | -7111 | 0.612257 | -3373 | 21211 | 2 | 0 | 0 | 0 | NO |
| 7 | ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_30S | -9156 | -7112 | 0.612231 | -3373 | 21212 | 2 | 0 | 0 | 0 | NO |
| 8 | ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_60S | -9164 | -7120 | 0.612024 | -3374 | 21220 | 2 | 0 | 0 | 0 | NO |
| 9 | ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_60S | -9165 | -7121 | 0.611998 | -3374 | 21221 | 2 | 0 | 0 | 0 | NO |

## Expanded signals

| Rank | Variant | Net PnL | vs no re-entry | PF | Avg loser | Max DD | Reentries | Profitable | Recovered to profit | Winners harmed | LOTO robust |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_30S | -21203 | -7332 | 0.6151 | -2754 | 37765 | 5 | 2 | 0 | 0 | NO |
| 2 | ONE_REENTRY_RECOVERY_50PCT_R_CONFIRM_0S | -22581 | -8710 | 0.600089 | -2823 | 37755 | 4 | 1 | 0 | 0 | NO |
| 3 | ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_0S | -23486 | -9615 | 0.590622 | -2868 | 40852 | 8 | 3 | 0 | 0 | NO |
| 4 | ONE_REENTRY_RECOVERY_50PCT_R_CONFIRM_60S | -24062 | -10191 | 0.584751 | -2897 | 41771 | 3 | 0 | 0 | 0 | NO |
| 5 | ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_0S | -25271 | -11400 | 0.5728 | -2958 | 42234 | 5 | 1 | 0 | 0 | NO |
| 6 | ONE_REENTRY_RECOVERY_50PCT_R_CONFIRM_30S | -27289 | -13418 | 0.553905 | -3059 | 41265 | 4 | 0 | 0 | 0 | NO |
| 7 | ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_60S | -28283 | -14412 | 0.545048 | -3108 | 42259 | 4 | 0 | 0 | 0 | NO |
| 8 | ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_60S | -32423 | -18552 | 0.511017 | -3315 | 46191 | 7 | 0 | 0 | 0 | NO |
| 9 | ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_30S | -32592 | -18721 | 0.509718 | -3324 | 47758 | 8 | 0 | 0 | 0 | NO |

## Finding

- No-reentry base reference: -2044 TWD, PF 0.876121.
- Best base-signal variant: ONE_REENTRY_RECOVERY_00PCT_R_CONFIRM_0S at -4163 TWD (-2119).
- No-reentry expanded reference: -13871 TWD, PF 0.709538.
- Best expanded variant: ONE_REENTRY_RECOVERY_20PCT_R_CONFIRM_30S at -21203 TWD (-7332).
- Classification: NO_RECOVERY_REENTRY_EDGE.
- Every tested base-signal re-entry variant was worse than no re-entry; this experiment rejects adding recovery re-entry to paper or production behavior.
- No production or live behavior changed.

## Limitations

- Only ten base signals and eighteen overlapping near-misses are available.
- Re-entry recovery thresholds are a fixed diagnostic grid, not fitted production parameters.
- Historical sessions before permanent 0050 collection cannot use a benchmark recovery gate.
- The 20260930 session is stitched and has four untimestamped callback errors.
- Independent signals and near-misses are not one executable 190,000 TWD portfolio.
