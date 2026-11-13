# ALL_EARLY_READY_SIGNAL_SELECTION_QUALITY_V1

This is a post-outcome exploratory, broker-free diagnostic. It labels every
first-per-symbol `EARLY_ENTRY_READY_OBSERVED` candidate independently; it does
not select only the first candidate of a day and does not sum overlapping P&L
as shared-capital account performance.

The frozen entry is the causal observer reference price with the existing
TWD190,000 whole-lot affordability rule. Outcomes use only best bids already
received at 1, 3, 5, 15, and 30 minutes. The 30-minute path records after-cost
MFE, MAE, time to cover costs, and time to TWD3,500 (the existing 1R reference).

Labels are fixed before this implementation reads the outcome files:

- `STRONG_RISE_1R`: 30-minute after-cost MFE is at least TWD3,500.
- `COST_COVERING_RISE_BELOW_1R`: MFE is positive but below TWD3,500.
- `NO_COST_COVERING_RISE`: MFE never becomes positive within 30 minutes.
- `DATA_INSUFFICIENT`: the path cannot be evaluated.

Feature tables are descriptive only: frozen 5-minute relative-strength buckets,
market regime, signal time, within-day ready rank, entry-price bucket, and
observation source. The existing 1% challenger is audited as a gate, including
strong-rise false negatives below 1%. No best bucket is promoted to production.

All source dates were already observed. Results are not out-of-sample evidence,
not account P&L, and not authorization to change LIVE. The workflow remains
`SHADOW_ONLY`, `actual_orders=0`, `actual_fills=0`, `broker_order_calls=0`.
