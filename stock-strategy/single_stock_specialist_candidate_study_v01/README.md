# Single-Stock Specialist Candidate Study V0.1

`SINGLE_STOCK_SPECIALIST_CANDIDATE_STUDY_V0_1` is **SPECIALIST CANDIDATE SCREENING**. It is not a trading strategy, entry model, exit model, buy/sell backtest, Stage A revision, or live system.

The fixed candidates are 2408 南亞科, 2344 華邦電, 3231 緯創, 3017 奇鋐, and 2368 金像電. No security may be added or replaced after results are seen.

## Research discipline

The discovery ranking uses only 2020–2022 (`HISTORICAL_DISCOVERY`). The 2023–2024 period is `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`, and 2025 is `STRESS_PREVALENCE_SEEN_NOT_BLIND`. Neither later period may rerank candidates, change weights, or change thresholds. They only apply the pre-registered structural stability gate.

The score is fixed at 30% volatility opportunity, 25% liquidity, 20% continuity, 15% behavior stability, and 10% gap safety. Components are cross-sectional percentile ranks among the five fixed candidates using discovery data only. Volatility opportunity is the equal, non-tuned mean of the ATR14 and absolute-return percentiles. Behavior stability is the inverse percentile of the equal, non-tuned mean of the three annual coefficients of variation for median ATR14%, median absolute return, and median turnover in 2020, 2021, and 2022.

Forward 10-session MFE, MAE, Day-10 return, +8% hit frequency, and -5% hit frequency are descriptive only. They never enter the candidate score (`future_outcomes_used_in_candidate_score = false`).

The selected stock means only “most worthy of the next single-stock time-series strategy research stage.” It does **not** mean “the stock most likely to rise in the future.” This module creates no entry/exit rules and performs no model fitting.

## Data and safety

The study reuses `surge_event_study_v01.data.load_ohlcv` and `prepare_stocks`, including the union market calendar, 0050 suspension handling, discontinuity segmentation, formal input hashes, and existing corporate-action fail-closed limitations. Metrics and forward paths never cross a discontinuity segment.

Formal inputs are resolved from the existing `winner_coverage_taxonomy_v01/run_manifest.json`, and every declared hash is reverified before analysis. Formal artifacts are immutable: if any output already exists, `publish` refuses to overwrite it.

Safety invariants are `actual_orders = 0`, `actual_fills = 0`, `broker_connections = 0`, `model_fit_count = 0`, and `stage_a_refit_count = 0`. The module has no broker connection and does not touch Stage A, N Compact, intraday runtime, prospective runtime, or prospective ledgers.

## Run

From `stock-strategy/`:

```bash
python3 -m single_stock_specialist_candidate_study_v01.main publish
pytest -q single_stock_specialist_candidate_study_v01/tests
```

Formal outputs are `candidate_period_metrics.csv`, `annual_metrics.csv`, `discovery_ranking.csv`, `stability_confirmation.csv`, `candidate_summary.json`, `data_audit.json`, and `run_manifest.json`.
