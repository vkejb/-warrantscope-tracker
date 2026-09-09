# UPSIDE_OPPORTUNITY_RANKING_V0_1

這是獨立的兩階段研究模組，不是交易策略，也沒有券商、委託或成交路徑。

Stage A `LINEAR_RIDGE_MFE10` 專門預測以 T+1 regular-session Open 為基準、Day1 至 Day10 Close 的最大有利變動。Primary opportunity pool 固定為每日 Top30；Top10、Top20、Top50 只作診斷。Stage B 不重訓，逐位重用已發布 `cross_sectional_alpha_ranking_v01` 的 same-day percentile matrix、`LINEAR_RIDGE_NET_RETURN` coefficients、intercept 與 score。Primary final candidate 固定為 Stage A Top30 內的 Stage B Top5。

## Frozen discipline

- 母體、outcomes、39 個 causal features、成本、公司行動切段與 0050 benchmark 都直接重用既有 immutable stores。
- 2020–2022 是 historical discovery；2023–2024 是 retrospective confirmation，不能稱 blind OOS；2025 是 stress / prevalence-seen。
- 2026-09 以後完全排除 model fit、feature selection 與 threshold selection。
- Stage A alpha 只可從 `{0.1, 1, 10, 100}`，依 discovery walk-forward daily MFE10 IC 選定。
- Stage B refit count 與 later-period refit count 都必須為 0。
- N Compact 保持 `pivot_separation_sessions <= 7 and bottom_difference > 0`，只作 overlay。
- BIAS 只作既有 multivariate context，不搜尋 threshold。

## Outcome and diagnostics

Primary Stage A target 是 Close-based MFE10，沒有加入未來 MAE。正式表同時報 MFE5、MAE5/10、+8/+10/+15 before -5、common-outcome gross/net return、PF 與 Top1%/Top5% tail removal。

Complementarity 使用 same-day Spearman/Pearson、top-decile overlap 與 Stage A Top30/Stage B Top10 overlap。2×2 quadrant 使用固定 same-day median split。Two-stage Top5 另有同股票 10 market-session cooldown proxy。

TAIEX point-in-time series 不存在於本研究重用的 immutable mother store；regime 表沿用上一輪已發布的 causal 0050 proxy，並在 manifest 明確標示，不以替代假資料冒充 TAIEX。

所有正式 CSV 使用 UTF-8 BOM、穩定欄位與空白缺值；`run_manifest.json` 保存 source/input/output hashes。Observation-level store 留在 `runtime/`，不提交 Git。

從 `stock-strategy/` 執行：

```bash
python3 -m upside_opportunity_ranking_v01.main
python3 -m unittest discover -s upside_opportunity_ranking_v01/tests -p 'test_*.py'
```

正式輸出存在時程式會拒絕覆寫。最終分類只能是 `PROMISING_FOR_TWO_STAGE_PROSPECTIVE_SHADOW`、`UPSIDE_SIGNAL_FOUND_BUT_NOT_TRADEABLE`、`DESCRIPTIVE_ONLY` 或 `NO_STABLE_UPSIDE_EDGE`。

安全不變量：`actual_orders = 0`、`actual_fills = 0`、`broker_connections = 0`。
