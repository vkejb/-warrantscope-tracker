# PREMARKET_POOL_FILTER_DIAGNOSTIC_V1 — frozen 2026-11-15

SHADOW_ONLY / NO_LIVE_ORDER / actual_orders=0. This study must not alter the installed expanded-universe builder, its seals, production LONG, quote subscriptions, order adapter or schedule.

Use each archived, hash-valid expanded-universe seal **before** the target session, not a today's Top30 list or a newly rebuilt universe. The seal source date is the prior trading day and its creation time must precede the next-session open. The existing first layer already has a 190 TWD maximum close, 30 million TWD minimum turnover, industry exclusions and 400-symbol cap. Run only these four fixed diagnostic variants on the same sealed stocks: BASE (none), CHART (prior close > MA5 > MA20), CHIP (previous-session combined institutional net shares > 0), and CHART_AND_CHIP. No other indicator or threshold search in this round.

Chart uses only 20 daily bars ending on the seal's source date. Chip uses the official TWSE/TPEx file for that same date. The historical chip files here were downloaded **after** the target sessions, so their values are retrospective, not verified presignal availability. Unknown chart/chip evidence is counted separately and cannot silently pass a gate. Do not substitute a newer official file for an older date.

For every seal stock, report pass/reject/unknown by variant. Join the full 365 archived first-per-symbol early-ready events and their original-time 5/15/30-minute labels to show how many ready events, existing 16 delayed-rule survivors, and nine original BASE account selections would have been removed. These labels are not strategy returns or proof of fill. Removing a stock before subscription changes callback capacity and subsequent observed candidates, so the retained subset of old signals is **not** a full counterfactual account replay. Show first-ready selection changes only as a diagnostic; no counterfactual P&L sum.

The earliest possible prospective test requires a source-date seal, source-date daily bars and both official institutional files captured before the target session, followed by a full target quote run. A missing item is PENDING/UNKNOWN, not a failed stock or no-signal day. Results on these 18 known dates cannot justify production gating.

Reproduce offline with `PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.premarket_pool_filter_study`. Outputs are private under `premarket_pool_filter_results/`.
