# Stock-Specific Setup Discovery V0.1

`STOCK_SPECIFIC_SETUP_DISCOVERY_V0_1` asks whether each stock in the frozen 15-stock Specialist Universe has its own interpretable daily entry setup. Every stock is researched independently. There is no pooled model, Stage A ranking, sector rotation, warrant selection, intraday timing, machine learning, exit optimization, or live trading.

The universe is loaded without modification from `specialist_universe_discovery_v01/final_specialist_universe.csv`, restricted to `CORE_SPECIALIST` and `REGIME_SPECIALIST`, and verified against its published hash. The universe file hash and a content snapshot hash are recorded in `run_manifest.json`.

## Frozen research discipline

All 16 variants across Breakout, Trend Pullback, Mean Reversion, Volatility Contraction/Expansion, and Gap Behavior are preregistered in `setup_definitions.json` and `analysis_spec.json` before formal outcomes. Signals use T regular-session OHLCV and earlier only. Prior highs exclude T. Entry is always T+1 regular-session Open; primary exit is Day5 Close and secondary exit is Day10 Close. No setup may cross a `prepare_stocks` discontinuity segment or a period boundary.

Each stock/setup variant is evaluated independently. While its Day5 position is active, repeated signals are ignored; a new T-close signal may be accepted on the previous trade's Day5 close because its entry occurs the next session. Raw signals, de-overlapped signals, and complete evaluable trades are reported separately.

Net returns use the repository's formal V2.1 Taiwan-stock model: NT$30,000 notional, 0.1% baseline slippage each way, discounted commission `0.001425 × 0.28` per side with NT$1 minimum, and 0.3% sell tax. Exit rules and costs are not optimized.

Only 2020–2022 `HISTORICAL_DISCOVERY` can select a setup. A candidate needs at least 30 de-overlapped trades, positive Day5 net mean, Day5 net PF above 1.05, positive annual net mean in at least two discovery years, Top-5%-winner-removed net PF of at least 0.90, and no single positive calendar quarter contributing over 75% of positive-quarter net return. If several pass, the frozen ordering is minimum annual net mean, overall PF, mean, trade count, then setup ID.

The frozen setup is applied unchanged to 2023–2024 `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS` and 2025 `STRESS_PREVALENCE_SEEN_NOT_BLIND`. Calendar-month cluster bootstrap uses 5,000 resamples. Market context is descriptive only and never becomes an entry gate.

## Multiple testing and limitations

This is explicitly `MULTIPLE_HYPOTHESIS_RESEARCH`: 15 stocks and 16 predefined variants create 240 discovery hypotheses. A discovery winner is not OOS evidence. Evidence comes only from frozen 2023–2024 and 2025 confirmation, and even those periods are not blind OOS.

MFE, MAE, +8%-before--5%, downside-first, tail removal, family matrix, Core/Regime metadata, and market-context splits do not affect discovery setup selection. Daily OHLCV cannot establish intraday ordering, so path thresholds use session Close returns relative to T+1 Open.

Safety invariants are `actual_orders = 0`, `actual_fills = 0`, `broker_connections = 0`, and `stage_a_refit_count = 0`. The study does not connect Yuanta, alter prospective ledgers, modify N Compact, modify Stage A, modify intraday runtime, change the Specialist Universe, or perform any warrant research.

Formal publication is fail-closed: existing artifacts are never overwritten.
