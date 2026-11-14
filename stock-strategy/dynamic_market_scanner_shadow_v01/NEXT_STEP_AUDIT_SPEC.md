# Next-step audit — frozen 2026-11-15

SHADOW_ONLY / NO_LIVE_ORDER / actual_orders=0. This is an offline evidence check, not a change to production LONG, an order policy, or a scheduler. It neither downloads market data nor starts a quote collector.

## 2026-11-16 readiness

The previous trading session must match the active calendar. Both official institutional files must have matching source/target dates, raw SHA-256 and a capture timestamp before target 09:00 Taipei. The active, hash-verified daily archives must contain the previous session's bar. The expanded-universe seal must exist and validate under its frozen policy. A target-session quote run must later exist and receive a separate quality review before any outcome is scored. Missing inputs remain `PENDING`/`NA`; no missing file is interpreted as a rejected signal.

## No-wait chip ordering

Population and baseline are the existing `CURRENT_RS1_180S` eligible early-ready signals in the known chart/chip research. Preserve original signal effective time, 190,000 TWD and one account trade per day. A chip-positive candidate may move ahead only when effective times are *exactly equal*. Within such a tie, use positive chip sign first, then original descending five-minute RS and stock ID. Missing chip data is not assumed positive. Later signals may never preempt an earlier eligible signal. Reuse the old account P&L only when the selected trade and full ordering are unchanged; otherwise mark `REPLAY_REQUIRED`. This test cannot measure a discretionary wait-for-better-stock policy or a different five-minute auction.

All 2026-10-06–11-11 inputs are known exploratory dates. Historical chip downloads occurred after the signals; even an unchanged ordering is not forward validation. The existing hard-veto `CHIP` variant is retained only as a prior exploratory comparison, not promoted to production. No new chip threshold or exit parameter is introduced here.

## Exit comparison boundary

The existing flow/breakeven study uses a different 16-session entry population with baseline -5,732 TWD, whereas the chart/chip study uses 18 dates, nine selected trades and baseline +5,899 TWD. It is not a paired exit comparison for these nine trades. No fixed-profit versus trailing-profit claim follows from joining those totals. A future paired exit study must freeze one entry/quantity cohort and use the same quote, fee, latency and missing-data model for all exits before computing results. Neither a profit target touch nor a cost-aware breakeven touch guarantees a fill.

Reproduce: `PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.next_step_audit 20261116`. Outputs are private/ignored under `next_step_audit_results/` and do not overwrite prior rounds.
