# RAW_SIGNAL_CHART_CHIP_COMPARISON_V1 — frozen 2026-11-15

Mode: SHADOW_ONLY, NO_LIVE_ORDER, actual_orders=0. Read archived research outputs and hashed quote runs only. Do not change production LONG, Stage A, orders, subscriptions or schedules; never overwrite prior studies.

Population is **all** 365 first-per-symbol `EARLY_ENTRY_READY_OBSERVED` events from the frozen 18-date `early_entry_backtest_results/signals.csv`, not only the 16 `CURRENT_RS1_180S` survivors. Events that never reached early-ready are outside this population and must not be called no-signal. Join by `(session_date, stock_id)` to the existing BASE candidate ledger; require exactly one row, unchanged upstream signal hash, and matching run manifest hash. Chart and chip values remain historical reconstructions; official institutional files were downloaded after signal time on these dates.

At each original ready time, use its archived `entry_price` and the existing 190,000 TWD whole-lot affordability rule. At +5/+15/+30 minutes, require a fresh best bid **and** trade tick at or before the exact checkpoint under the existing outcome freshness limit. Record the projected round-trip net P&L at the bid and trade-price change separately. A stale/missing mark is NA, never zero or future-filled. These are immediate-entry, fixed-horizon *signal labels*, not latency/depth-verified fills or rule-exit results. Sum of labels must never be reported as shared-capital account P&L.

Report every raw signal, funnel counts, and chip-positive/nonpositive/unknown groups with complete/partial/missing counts and matched-horizon mean/positive share. The group comparison is descriptive and confounded by date, symbol, timing and survivorship; it is not an optimized threshold or proof that chip selection works. Report overlap with the 16 survivors and original account selections separately. Do not call historical dates out-of-sample.

Reproduce: `PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.raw_signal_comparison`. Outputs are private under `raw_signal_comparison_results/`.
