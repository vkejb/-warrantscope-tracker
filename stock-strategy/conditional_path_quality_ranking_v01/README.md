# CONDITIONAL_PATH_QUALITY_RANKING_V0_1

這是獨立研究模組，不是交易策略，也沒有券商、委託或成交路徑。

研究直接載入已發布 `upside_opportunity_ranking_v01` 的 `LINEAR_RIDGE_MFE10` 模型、分數與名次。Stage A 不重新訓練，opportunity pool 固定為每天原始 Top30。Stage B 只在這 30 檔內，以 `LOGISTIC_RIDGE_PATH_SUCCESS` 預測 `+8% before -5% within 10 sessions`；未觸及兩道 barrier 的 timeout 固定納入 `NON_SUCCESS`。Primary selection 固定為 Conditional Top5，Top3 與 Top10 只作診斷。

## Evidence discipline

- 2019 只作 feature warm-up。
- 2020–2022 是 historical discovery。
- 2023–2024 是 retrospective confirmation，不能稱 blind OOS。
- 2025 是 stress / prevalence-seen。
- 2026-09 以後完全排除 model fit、feature selection 與 threshold selection。
- Logistic Ridge 的 `C` 只從 `{0.01, 0.1, 1, 10}` 依兩段 discovery walk-forward daily path IC 選定。
- Stage A refit count 與 later-period refit count 必須為 0。
- N Compact 保持 `pivot_separation_sessions <= 7 and bottom_difference > 0`，只作 overlay。

39 個基礎 feature 直接使用上一輪已發布的 same-day cross-sectional percentile matrix。額外兩個 contextual features 是 frozen Stage A score 的 discovery-only z-score，以及 Stage A full-market rank percentile。它們不會改變 Stage A pool。

## Outputs and audit

正式 CSV 使用 UTF-8 BOM、固定欄位與空白缺值。`conditional_model_spec.json` 保存模型、preprocessing、walk-forward 與係數穩定度；`run_manifest.json` 保存 immutable inputs、原始碼、正式 artifacts 及 protected ledgers 的 SHA-256。

TAIEX point-in-time series不在重用的 immutable mother store；market regime 診斷沿用既有 causal 0050 proxy，不能解讀為 TAIEX 結果。

從 `stock-strategy/` 執行：

```bash
python3 -m conditional_path_quality_ranking_v01.main
python3 -m unittest discover -s conditional_path_quality_ranking_v01/tests -p 'test_*.py'
```

正式輸出存在時程式會拒絕覆寫。最終分類只能是 `PROMISING_FOR_PROSPECTIVE_CONDITIONAL_SHADOW`、`PATH_SIGNAL_FOUND_BUT_NOT_TRADEABLE`、`DESCRIPTIVE_ONLY` 或 `NO_CONDITIONAL_PATH_EDGE`。

安全不變量：`actual_orders = 0`、`actual_fills = 0`、`broker_connections = 0`。
