# Indicator stop diagnostic

Uses causal Yuanta tick flow, large-trade flow and aggregated five-level imbalance.

| Variant | Status | Net | PF | Max DD | Indicator exits | Recovered later | Before 10:30 net | Before 10:30 PF |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| BASELINE_5000 | PREDECLARED | -6667.00 | 0.73 | 12865.00 | 0 | 0 | 1461.00 | 1.10 |
| ADVERSE_2OF3_2X_WHILE_LOSS | PREDECLARED | -12675.00 | 0.33 | 13268.00 | 13 | 6 | -6238.00 | 0.48 |
| ADVERSE_2OF3_2X_AFTER_1000_LOSS | PREDECLARED | -1171.00 | 0.94 | 9095.00 | 9 | 2 | 3764.00 | 1.35 |
| ADVERSE_3OF3_1X_WHILE_LOSS | PREDECLARED | -18523.00 | 0.22 | 18523.00 | 13 | 7 | -7941.00 | 0.40 |
| VWAP_BREAK_ADVERSE_2OF3_2X | PREDECLARED | -4075.00 | 0.81 | 11999.00 | 4 | 1 | 2557.00 | 1.20 |
| BOOK_DEPLETION_ADVERSE_2OF3_2X | PREDECLARED | -6021.00 | 0.67 | 9564.00 | 7 | 2 | 1556.00 | 1.19 |
| VWAP_BREAK_ADVERSE_2OF3_2X_HARD_4000 | PREDECLARED | -3074.00 | 0.85 | 10998.00 | 4 | 1 | 3558.00 | 1.30 |
| ADVERSE_2OF3_2X_AFTER_1000_LOSS_NO_POSITIVE_MFE | EXPLORATORY_POST_HOC | -1171.00 | 0.94 | 9095.00 | 9 | 2 | 3764.00 | 1.35 |
| VWAP_BREAK_ADVERSE_2OF3_2X_NO_POSITIVE_MFE | EXPLORATORY_POST_HOC | -4075.00 | 0.81 | 11999.00 | 4 | 1 | 2557.00 | 1.20 |
| VWAP_BREAK_ADVERSE_2OF3_2X_NO_POSITIVE_MFE_HARD_4000 | EXPLORATORY_POST_HOC | -3074.00 | 0.85 | 10998.00 | 4 | 1 | 3558.00 | 1.30 |

## Diagnostic readout

- Pure VWAP plus consecutive 2-of-3 adverse flow reduced sampled loss by 38.9%, from -6667.00 to -4075.00 TWD, but remained negative.
- The same evidence with a 4,000 TWD disaster cap reduced sampled loss by 53.9%, to -3074.00 TWD.
- The best predeclared loss result was the 1,000 TWD activation-zone variant at -1171.00 TWD and PF 0.94; this still uses a fixed-money gate and therefore is not a pure indicator stop.
- Largest premature cut: 20260923 2221 changed from 478.00 to -3014.00 TWD, and later recovered positive.
- The post-hoc no-positive-MFE check changed no exit because that recovering trade had not yet shown positive net PnL when the early indicator fired.

## Data boundary

Used: saved Yuanta tick price/volume/bid/ask/in-out flag and aggregated five-level bid/ask volume.
Not used: newly audited GetStockInformation/GetWatchListAll fields and per-level queue changes, because they were not saved in these historical sessions.

The fixed amount is only a disaster cap; indicator variants trigger from market evidence first.
Variants marked EXPLORATORY_POST_HOC were added after reviewing the first comparison and require new out-of-sample sessions.
No production or live behavior changed.
