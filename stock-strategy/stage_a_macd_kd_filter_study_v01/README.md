# Stage A MACD/KD fixed-filter study V0.1

This research-only module reuses the published frozen Stage A Top30 without refit. It compares a no-filter baseline, the existing previous-session foreign-positive reference, fixed standard MACD/KD entry filters, and their intersection. It does not search indicator parameters or thresholds.

- MACD: 12/26/9, pass when DIF is above its signal and above zero.
- KD: 9/3/3, pass when K is above D and below 80.
- Entry: T+1 open after T-close signal/filter, 0.1% one-way slippage.
- Position size: `min(NAV/30, TWD2,000)`, integer odd-lot shares.
- No repeat addition. Filters apply only when opening a position. Exit at the next open after the name leaves Stage A Top30.
- Costs: existing discounted commission with TWD1 minimum and 0.3% sell tax.

The result is descriptive because the indicator definitions were requested after prior Stage A results had already been reviewed. It is not a validated strategy and creates no broker connection, order, or fill.
