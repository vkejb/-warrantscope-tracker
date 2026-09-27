# Intraday loss-reduction diagnostic

This is a fixed-rule diagnostic over overlapping independent first signals, not an executable portfolio.

| Variant | Trades | Net PnL | PF | Max DD | Net less largest winner | Positive days |
|---|---:|---:|---:|---:|---:|---:|
| BASELINE | 18 | -6667.00 | 0.73 | 12865.00 | -11910.00 | 1 |
| NO_NEW_AFTER_1030 | 12 | 1461.00 | 1.10 | 5861.00 | -3782.00 | 1 |
| SHORT_ONLY_DIAGNOSTIC | 10 | 6716.00 | 1.83 | 5754.00 | 1473.00 | 2 |
| SHORT_BEFORE_1030_DIAGNOSTIC | 6 | 7277.00 | 2.40 | 5193.00 | 2034.00 | 2 |
| LONG_ONLY_CURRENT_PRODUCTION | 8 | -13383.00 | 0.21 | 13383.00 | -14460.00 | 0 |
| LONG_BEFORE_1030 | 6 | -5816.00 | 0.37 | 5816.00 | -6893.00 | 0 |

## Conclusion

The only positive candidate after removing its largest winner is SHORT_BEFORE_1030_DIAGNOSTIC. It is not production-ready because short eligibility was not captured and the sample is only three partial sessions. The long-only cohort remains negative; the defensible live action is no promotion, not parameter fitting.

All variants remain in research/shadow mode. No live setting or order path was changed.
