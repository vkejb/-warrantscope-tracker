# Specialist Universe Discovery V0.1

`SPECIALIST_UNIVERSE_DISCOVERY_V0_1` builds a small, behaviorally diverse **SPECIALIST UNIVERSE**. It does not identify a unique winner and is not a sector-rotation model, entry strategy, exit strategy, direction model, intraday system, or live-trading system.

All eligibility, normalization, quality scoring, K=8 behavior clustering, representative selection, correlation measurements, and discovery ordering use only 2020–2022 `HISTORICAL_DISCOVERY`. The 2023–2024 period is `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`; 2025 is `STRESS_PREVALENCE_SEEN_NOT_BLIND`. Later periods cannot recluster, rerank, change weights, change thresholds, or select later-period winners.

## Frozen design

The module reuses `surge_event_study_v01.data.load_ohlcv` and `prepare_stocks`, including the ordinary four-digit proxy, 0050-aware union calendar, discontinuity segmentation, existing corporate-action limitation, and immutable input hashes. Financial, traditional-industry, shipping, telecom, consumer, plastics, high-price, and non-electronics stocks are not excluded by category.

Eligibility requires each discovery year to have at least 90% coverage and 180 sessions, discovery median turnover of NT$200 million, median ATR14 of 1%, median close of NT$10, P95 absolute gap no more than 8%, and no discovery missing run longer than 20 market sessions. Thresholds are never relaxed to reach a target pool size.

Quality score weights are fixed at 25% liquidity, 20% continuity, 20% annual behavior stability, 15% capped-percentile tradable volatility, 10% gap safety, and 10% trend/structure quality. The complete pre-ranking formulas are in `analysis_spec.json`.

KMeans uses exactly eight clusters. Its ten behavior features are winsorized on the eligible discovery cross-section at 2.5%/97.5%, population-z-scored, and clustered using deterministic farthest-first initialization and Lloyd updates. Industry and liquidity are not cluster features. Cluster labels describe the resulting centroids; they are not selection inputs.

Each cluster contributes its highest-quality eligible stock. Its second-ranked stock may be considered only when discovery daily-return correlation with the primary is below 0.75. There are at most two selections per cluster and 15 overall. A pool below 10 is reported without loosening any gate.

Forward T+1 Open to Day1–Day10 Close MFE, MAE, Day10 return, +8% hits, and -5% hits are descriptive only. `future_outcomes_used_for_universe_selection` is always false.

## Stability and safety

A behavior shift does not automatically remove a stock. A discovery selection is `CORE_SPECIALIST` only when both fixed later-period gates pass. If tradability remains but behavior shifts, it is `REGIME_SPECIALIST`. `EXCLUDED_AFTER_STABILITY` is reserved for fixed severe coverage, liquidity, long-suspension/data, or extreme-gap failures.

No reliable repository industry master was available. `current_industry_descriptive` is therefore blank with `UNAVAILABLE_NO_RELIABLE_REPOSITORY_SOURCE`; industry is not backfilled and never affects score, cluster, or selection.

Safety invariants are `actual_orders = 0`, `actual_fills = 0`, `broker_connections = 0`, and `stage_a_refit_count = 0`. The module does not modify Stage A, N Compact, intraday runtime, prospective runtime, or prospective ledgers.

Formal publication is fail-closed: if any published artifact already exists, the module refuses to overwrite it.

Run from `stock-strategy/` with the repository dependency runtime:

```bash
/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m specialist_universe_discovery_v01.main publish

/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m unittest discover -s specialist_universe_discovery_v01/tests -v
```
