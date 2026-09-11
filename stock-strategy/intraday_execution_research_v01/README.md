# INTRADAY_EXECUTION_RESEARCH_V0_1

This is not a trading strategy. It is a mock-only, vendor-neutral data and
research foundation for:

`Frozen Stage A Top30 -> T+1 intraday observation -> future entry-timing research`

There is no broker SDK, login, account, order, fill, or execution adapter in
this module. The only adapter is `MockQuoteAdapter`. If quote permission is
approved in the future, a separate `yuanta_quote_adapter.py` may implement the
frozen `QuoteAdapter` protocol. Real credentials must never be committed.

## Frozen contracts

- Watchlists load the exact published Stage A score/rank store and verify its
  model fingerprint and three artifact hashes. Stage A is never recomputed or
  refit.
- A signal-date watchlist contains exactly the published Top30, is sorted by
  frozen rank, and has a deterministic SHA-256. It is the subscription universe
  for the following market session.
- Tick identity preserves `stock_code` as a string. Exchange event, local
  receipt, and processing timestamps are separate timezone-aware values.
- Raw JSONL is crash-safe and append-only. Duplicate identity is
  `(stock_code, exchange_timestamp, sequence_id)` or the full event fingerprint
  when sequence ID is absent. A sealed day cannot accept new ticks.
- 1-minute and 5-minute bars are deterministic transformations of raw events.
  No future event can modify an earlier bucket.
- Frozen snapshot times are 09:05, 09:15, 09:30, 10:00, 11:00, and 13:00.
  Entry proxy is the first valid event strictly after the snapshot.
- `vwap_slope_5m` compares the current trailing five-minute VWAP with the
  immediately preceding five-minute VWAP. `intraday_drawdown_from_open` uses
  the lowest observed trade through the snapshot relative to the opening
  trade; it is not the latest return from open.
- Unavailable market, Top5 book, or historical intraday-volume context remains
  missing and is explicitly quality-flagged. Important prices are never
  forward-filled.

## Commands

```bash
PYTHONPATH=stock-strategy python3 -m intraday_execution_research_v01.main healthcheck
PYTHONPATH=stock-strategy python3 -m intraday_execution_research_v01.main prepare-watchlist --date 2025-12-30
PYTHONPATH=stock-strategy python3 -m intraday_execution_research_v01.main run-mock-session --date 2025-12-30
PYTHONPATH=stock-strategy python3 -m intraday_execution_research_v01.main replay --date 2025-12-31 --speed instant
```

The `--date` for watchlist/mock preparation is the frozen Stage A signal date.
The watchlist records the following frozen market session as its subscription
trading date. Replay uses the raw trading date.

## Raw schema and storage

Raw ticks are written under
`runtime/intraday_raw/YYYYMMDD/<stock_code>.jsonl`. The schema reserves five
bid/ask levels even when the mock source provides only Top1. A daily seal binds
the watchlist, every raw file, 1m/5m bars, feature snapshots, counts, missing
symbols, quality diagnostics, and hashes.

The mock feed deterministically covers normal events, one exact duplicate,
receive-order anomalies, a reconnect, temporary silence, missing Top1 book,
halt-like pauses, and burst volume. It is test infrastructure only and provides
no evidence about profitability or real entry quality.

## Outcome boundary

The module preregisters schemas for same-day return/MFE/MAE, next-open return,
Day1-5 and Day1-10 MFE/MAE, +8/-5, +5/-3, and downside-first. It does not run
these statistics without genuine prospective intraday observations.

The only valid current classification is:

`INFRASTRUCTURE_READY_NO_REAL_INTRADAY_EVIDENCE`

Safety counters remain `actual_orders=0`, `actual_fills=0`, and
`broker_connections=0`.
