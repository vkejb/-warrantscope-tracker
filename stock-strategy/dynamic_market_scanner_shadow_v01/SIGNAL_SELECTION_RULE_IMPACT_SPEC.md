# SIGNAL_SELECTION_RULE_IMPACT_V1

Known-archive exploratory comparison frozen before running this implementation:

- `CURRENT_RS1_180S`: relative strength at least 1%, then wait 180 seconds and
  require the original reference entry to show positive after-cost hypothetical
  P&L. Entry uses the fresh ask plus one tick at confirmation.
- `DUAL_LANE_PERSISTENCE_90S`: relative strength is retained for ranking but is
  not a hard rejection. At +30, +60, and +90 seconds, take the first checkpoint
  where the unchanged V2 recent-volume gate passes, the original breakout proxy
  remains held, and the existing opening/VWAP anti-chase limits still pass.

BASE and the existing five-second/two-tick adverse STRESS entry are both kept.
All-signal results use the after-cost best-bid mark 30 minutes after the actual
entry and are overlapping diagnostics, not account P&L. Account replay selects
the earliest confirmed candidate, breaks a tie by current relative strength,
and permits one trade/position per date with TWD190,000 and the unchanged exit
engine.

All 18 dates were already observed. Results are post-outcome exploratory, not
untouched validation and not LIVE approval. No broker module is imported or
called; `actual_orders=0`, `actual_fills=0`, `broker_order_calls=0`.
