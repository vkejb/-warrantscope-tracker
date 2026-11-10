# STRICT_MARKET_WIDE_V1_ABLATION_V1

This frozen diagnostic uses the already generated 51-candidate causal ledger.
It does not change any numeric threshold and does not search combinations.

- A0 keeps every strict gate.
- A1 removes only the positive after-cost progress gate.
- A2 removes only the non-negative 60-second signed-flow gate.
- A3 removes only the existing opening/VWAP anti-chase family.

Quote/book freshness, spread, TWD 190, whole-lot affordability, market context,
breakout, and VWAP safety gates are never removable. Each variant independently
chooses the earliest causal confirmation and keeps one trade per day. Exits and
costs are unchanged.

All 16 dates were already inspected. Results are exploratory attribution only.
No variant may be declared validated or promoted from these dates; the purpose
is to select at most one simple candidate for prospectively collected dates.
