# Early-stop recovery-grace validation

The recovery-aware 120-second stop is held briefly only when it would otherwise trigger. Hard stop, MFE protection, reversal and EOD exits remain active. A measured recovery cancels the pending stop once.

## Base signals

| Rank | Grace | Cancel recovery | Net PnL | vs immediate | PF | Avg loser | Max DD | Pending | Cancelled | Cancelled profitable | Winners harmed | LOTO robust |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 15s | 0.10R | -3940 | -1896 | 0.785823 | -2628 | 15996 | 5 | 0 | 0 | 0 | NO |
| 2 | 15s | 0.20R | -3940 | -1896 | 0.785823 | -2628 | 15996 | 5 | 0 | 0 | 0 | NO |
| 3 | 15s | 0.30R | -3940 | -1896 | 0.785823 | -2628 | 15996 | 5 | 0 | 0 | 0 | NO |
| 4 | 30s | 0.10R | -3940 | -1896 | 0.785823 | -2628 | 15996 | 5 | 0 | 0 | 0 | NO |
| 5 | 30s | 0.20R | -3940 | -1896 | 0.785823 | -2628 | 15996 | 5 | 0 | 0 | 0 | NO |
| 6 | 30s | 0.30R | -3940 | -1896 | 0.785823 | -2628 | 15996 | 5 | 0 | 0 | 0 | NO |
| 7 | 60s | 0.10R | -4140 | -2096 | 0.777371 | -2657 | 15996 | 5 | 0 | 0 | 0 | NO |
| 8 | 60s | 0.20R | -4140 | -2096 | 0.777371 | -2657 | 15996 | 5 | 0 | 0 | 0 | NO |
| 9 | 60s | 0.30R | -4140 | -2096 | 0.777371 | -2657 | 15996 | 5 | 0 | 0 | 0 | NO |

## Expanded signals

| Rank | Grace | Cancel recovery | Net PnL | vs immediate | PF | Avg loser | Max DD | Pending | Cancelled | Cancelled profitable | Winners harmed | LOTO robust |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 15s | 0.10R | -16764 | -2893 | 0.66901 | -2532 | 34673 | 14 | 0 | 0 | 0 | NO |
| 2 | 15s | 0.20R | -16764 | -2893 | 0.66901 | -2532 | 34673 | 14 | 0 | 0 | 0 | NO |
| 3 | 15s | 0.30R | -16764 | -2893 | 0.66901 | -2532 | 34673 | 14 | 0 | 0 | 0 | NO |
| 4 | 30s | 0.10R | -17662 | -3791 | 0.657355 | -2577 | 35171 | 14 | 0 | 0 | 0 | NO |
| 5 | 30s | 0.20R | -17662 | -3791 | 0.657355 | -2577 | 35171 | 14 | 0 | 0 | 0 | NO |
| 6 | 30s | 0.30R | -17662 | -3791 | 0.657355 | -2577 | 35171 | 14 | 0 | 0 | 0 | NO |
| 7 | 60s | 0.10R | -17711 | -3840 | 0.65673 | -2580 | 35370 | 14 | 0 | 0 | 0 | NO |
| 8 | 60s | 0.20R | -17711 | -3840 | 0.65673 | -2580 | 35370 | 14 | 0 | 0 | 0 | NO |
| 9 | 60s | 0.30R | -17711 | -3840 | 0.65673 | -2580 | 35370 | 14 | 0 | 0 | 0 | NO |

## Finding

- Immediate-stop reference: -2044 TWD, PF 0.876121, max drawdown 14100 TWD.
- Best base-signal grace: EARLY_STOP_GRACE_15S__CANCEL_RECOVERY_0.10R at -3940 TWD (-1896).
- Classification: NO_ROBUST_EARLY_STOP_GRACE_EDGE.
- It delayed 5 base-signal stops, cancelled 0, and improved 0 trades while worsening 3.
- 2221 never met even the smallest 0.10R recovery threshold inside 15/30/60 seconds; the grace exit lost more before the later rally.
- This result does not change production, paper-shadow or live behavior.

## Material base-trade impacts for the least-damaging grace

| Symbol | Immediate PnL | Grace PnL | Difference | Full-path MFE | Grace exit |
|---|---:|---:|---:|---:|---|
| 3016 | -2546 | -3543 | -997 | -52 | DISASTER_STOP_NEG_1R |
| 2221 | -2016 | -2516 | -500 | 5465 | RECOVERY_GRACE_EXPIRED |
| 4956 | -2795 | -3194 | -399 | -1199 | RECOVERY_GRACE_EXPIRED |

## Limitations

- Only ten base signals and eighteen overlapping near-misses are available.
- The fixed grace grid is diagnostic and was not optimized on a separate test set.
- Older independent signals and near-misses are not one executable portfolio.
- The 20260930 session is stitched and has four untimestamped callback errors.
