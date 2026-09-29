# Anti-chase diagnostics v0.1

Backtest-only filters designed to identify signals that are already excessively
extended at entry. Features are causal and side-aware: distance from VWAP,
one- and five-minute directional return, opening extension, breakout overshoot,
and flow saturation.

Results are diagnostic only. No filter is enabled in production and the
post-hoc score threshold is explicitly excluded from recommendation.
