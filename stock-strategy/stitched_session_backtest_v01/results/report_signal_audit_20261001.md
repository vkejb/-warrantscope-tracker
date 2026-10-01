# 2026-10-01 signal and counterfactual audit

## Data integrity

- Authoritative LIVE archive: `20261001T004849.174778Z_feb27b61`
- Status: `COMPLETE`
- Callback errors: 0
- Top30 ticks / books: 217,823 / 393,309
- 0050 ticks / books: 8,490 / 21,179
- All 30 candidate symbols contain both tick and book data.
- All 491 expected entry decision windows were replayed.

## Signal result

- 456 windows contained no base candidate.
- 35 windows failed closed because the 0050 benchmark tick was stale.
- A benchmark-free audit found zero base candidates in those 35 windows.
- Approved entries: 0.
- Two raw candidates appeared, both in 3094 聯傑.

## Counterfactual entries

| Time | Entry | Qty | Original gate | Exit | Exit reason | Net PnL | MFE | MAE |
|---|---:|---:|---|---:|---|---:|---:|---:|
| 09:37:30 | 64.20 | 2,000 | Anti-chase opening extension | 66.50 | MFE profit protection | +4,176 | +6,171 | -1,212 |
| 10:18:00 | 67.80 | 2,000 | Missing 5-minute stock history | 66.20 | Stop loss | -3,629 | -1,035 | -3,629 |

The first candidate also exceeded the VWAP-extension cap and had only one of two required confirmations. The second candidate was 9.55% above the opening reference and 4.35% above VWAP, so it would also have failed the anti-chase overlay after history became available.

With the current one-trade-per-day limit, forcing the first chronological candidate would have produced NT$4,176 after estimated commission and day-trade tax. The arithmetic sum of both independent paths is NT$547, but it is not a feasible production-day result because the first trade would consume the daily trade allowance.

This is a counterfactual diagnostic only. It did not connect to the broker, submit an order, or alter LIVE strategy behavior.
