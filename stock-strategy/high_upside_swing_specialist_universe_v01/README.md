# High-Upside Swing Specialist Universe V0.1

`HIGH_UPSIDE_SWING_SPECIALIST_UNIVERSE_V0_1` discovers a small Taiwan-stock universe whose underlying shares repeatedly produced large directional moves over five to ten sessions. It starts from the repository's full ordinary four-digit stock proxy. Industry labels, the previous 15-stock universe, prior setup results, Stage A, N Compact, intraday data, and warrant data do not select stocks.

This is universe characterization, not an entry strategy, exit strategy, return backtest, sector-rotation model, or live-trading system. A selected stock historically offered price movement; selection does not establish that any trading rule can capture it.

## Frozen discovery discipline

Eligibility, forward-path characterization, percentile normalization, Swing Score, repeatability, discovery ranking, correlation de-duplication, Primary Pool, and reserve ordering use only 2020–2022 `HISTORICAL_DISCOVERY`. The analysis specification is frozen in `analysis_spec.json` before formal results.

Every eligible T uses T+1 regular-session Open as the reference and Day1–Day10 Closes as the path. A path must stay in the named period and one `prepare_stocks` discontinuity segment. Discovery future paths are deliberately allowed because repeated upside movement is the universe characteristic being measured. The same outcomes from 2023–2024 and 2025 never alter discovery score, rank, or membership.

The Daily Movement Gate requires median ATR14 of at least 2.0% or median daily `(High-Low)/prior Close` of at least 2.5%. Daily volatility contributes only 5% of Swing Score. The primary components are de-overlapped +8%, +10%, and +15% episodes, MFE10, +8%-before--5%, and Swing Persistence.

For each upside threshold, an accepted episode start blocks the following ten market sessions for that stock and threshold. T+11 may start another episode. Repeatability requires at least two discovery years with at least three +8% episodes and two +10% episodes. No threshold is relaxed to fill the pool.

Swing Persistence is the median absolute Day10 Close return from T+1 Open divided by `sqrt(10) × median daily range`. It distinguishes multi-session close displacement from daily high-low noise. MAE and downside-first outcomes remain descriptive and never hard-exclude a liquid, data-valid stock.

Primary selection walks the repeatability-passed Swing Score ranking from the top. A candidate joins only when its discovery daily-return correlation with every selected stock is at most 0.80. Correlation rejects enter `HIGH_CORRELATION_RESERVE`; the next 15 non-primary names by frozen score form the general reserve. Behavior clusters do not grant seats.

## Later-period interpretation

The selected pool is held fixed for 2023–2024 `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS` and 2025 `STRESS_PREVALENCE_SEEN_NOT_BLIND`. Neither period is blind OOS. They only support `PERSISTENT_HIGH_UPSIDE_SPECIALIST`, `REGIME_HIGH_UPSIDE_SPECIALIST`, `DISCOVERY_ONLY_HIGH_UPSIDE`, or `LOST_TRADABILITY` classification under the fixed rules in `analysis_spec.json`.

The comparison with `specialist_universe_discovery_v01` is descriptive and does not modify that immutable study. Formal artifacts are fail-closed and never overwritten.

Safety invariants are `actual_orders = 0`, `actual_fills = 0`, `broker_connections = 0`, `model_fit_count = 0`, and `stage_a_refit_count = 0`. This module does not connect Yuanta, alter prospective ledgers, modify Stage A, modify N Compact, modify intraday runtime, optimize an entry or exit, or perform warrant research.
