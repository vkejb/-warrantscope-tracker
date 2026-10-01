# Recovery-aware 120-second exit validation

At the one-time 120-second checkpoint, a losing/no-progress trade exits only when it has not recovered sufficiently from its post-entry MAE. Any earlier existing stop remains authoritative.

| Rank | Variant | Net PnL | vs baseline | W/L | PF | Avg loser | Max DD | Winners harmed |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_20PCT_R_NO_FLOW | -29482 | +20903 | 3/25 | 0.418891 | -2029 | 37212 | 0 |
| 2 | RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_30PCT_R_NO_FLOW | -29482 | +20903 | 3/25 | 0.418891 | -2029 | 37212 | 0 |
| 3 | RECOVERY_AWARE_120S_LOSS_20PCT_R_RECOVERY_20PCT_R_NO_FLOW | -29482 | +20903 | 3/25 | 0.418891 | -2029 | 37212 | 0 |
| 4 | RECOVERY_AWARE_120S_LOSS_20PCT_R_RECOVERY_30PCT_R_NO_FLOW | -29482 | +20903 | 3/25 | 0.418891 | -2029 | 37212 | 0 |
| 5 | RECOVERY_AWARE_120S_LOSS_30PCT_R_RECOVERY_20PCT_R_NO_FLOW | -29482 | +20903 | 3/25 | 0.418891 | -2029 | 37212 | 0 |
| 6 | RECOVERY_AWARE_120S_LOSS_30PCT_R_RECOVERY_30PCT_R_NO_FLOW | -30679 | +19706 | 3/25 | 0.409235 | -2077 | 38409 | 0 |
| 7 | RECOVERY_AWARE_120S_LOSS_40PCT_R_RECOVERY_20PCT_R_NO_FLOW | -32076 | +18309 | 3/25 | 0.398515 | -2133 | 39806 | 0 |
| 8 | RECOVERY_AWARE_120S_LOSS_40PCT_R_RECOVERY_30PCT_R_NO_FLOW | -33273 | +17112 | 3/25 | 0.389766 | -2181 | 41003 | 0 |
| 9 | RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_10PCT_R_NO_FLOW | -33773 | +16612 | 3/25 | 0.386224 | -2201 | 39308 | 0 |
| 10 | RECOVERY_AWARE_120S_LOSS_20PCT_R_RECOVERY_10PCT_R_NO_FLOW | -33773 | +16612 | 3/25 | 0.386224 | -2201 | 39308 | 0 |
| 11 | RECOVERY_AWARE_120S_LOSS_30PCT_R_RECOVERY_10PCT_R_NO_FLOW | -33773 | +16612 | 3/25 | 0.386224 | -2201 | 39308 | 0 |
| 12 | RECOVERY_AWARE_120S_LOSS_40PCT_R_RECOVERY_10PCT_R_NO_FLOW | -33773 | +16612 | 3/25 | 0.386224 | -2201 | 39308 | 0 |
| 13 | RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_20PCT_R_FLOW_2OF3 | -41008 | +9377 | 3/25 | 0.341343 | -2490 | 46543 | 0 |
| 14 | RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_30PCT_R_FLOW_2OF3 | -41008 | +9377 | 3/25 | 0.341343 | -2490 | 46543 | 0 |
| 15 | RECOVERY_AWARE_120S_LOSS_20PCT_R_RECOVERY_20PCT_R_FLOW_2OF3 | -41008 | +9377 | 3/25 | 0.341343 | -2490 | 46543 | 0 |

## Cohort comparison

### Base signals

- Baseline: -9627 TWD.
- Plain 120-second reference: -4437 TWD.
- Best recovery-aware result: -3939 TWD (RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_30PCT_R_NO_FLOW).

### Additional near-misses

- Baseline: -40758 TWD.
- Plain 120-second reference: -25543 TWD.
- Best recovery-aware result: -24346 TWD (RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_20PCT_R_NO_FLOW).

### Expanded unique signals

- Baseline: -50385 TWD.
- Plain 120-second reference: -29980 TWD.
- Best recovery-aware result: -29482 TWD (RECOVERY_AWARE_120S_LOSS_00PCT_R_RECOVERY_20PCT_R_NO_FLOW).

## Difference versus plain 120 seconds

- Expanded net improvement: +498 TWD.
- Changed trades: 3.

- 20260924 3605: plain 120s -585 TWD, recovery-aware -87 TWD (+498).
- 20260924 6456: plain 120s -929 TWD, recovery-aware -2126 TWD (-1197).
- 20260930 1709: plain 120s -2008 TWD, recovery-aware -811 TWD (+1197).

The improvement is small and includes one avoided exit that later lost more. It is suitable for shadow validation only, not production promotion.

## Limitations

- The recovery thresholds are a diagnostic grid, not fitted production parameters.
- Additional near-miss entries overlap and are not feasible portfolio PnL.
- The 20260930 session is stitched and has four untimestamped callback errors.
- Twenty-eight unique signals remain too small for production selection.

No production or live behavior changed.
