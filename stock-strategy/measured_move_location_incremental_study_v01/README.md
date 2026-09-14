# Measured Move Location Incremental Study v0.1

**CONCEPT_RECONSTRUCTION_NOT_AUTHOR_STRATEGY**

本研究只檢查一個問題：在相同、已預先凍結的日線 setup observation 中，訊號日價格位於 causal AB=CD measured move 的不同位置，是否對未來 5–10 日路徑提供增量資訊。它不是對任何作者完整策略、Signal K 或 anchor 規則的重現，也不是一套交易策略。

## Frozen research contract

- 資料沿用 `surge_event_study_v01.data.load_ohlcv` 與 `prepare_stocks`，保留 0050 calendar、discontinuity segmentation、corporate-action fail-closed 限制與正式 input hashes。
- Primary universe 是 `high_upside_swing_specialist_universe_v01` Discovery Primary Pool 中未失去可交易性的 9 檔；secondary robustness 是該研究固定 eligibility universe 的 133 檔。本研究不重新選 universe。
- Baseline signals 是 `stock_specific_setup_discovery_v01` 已 preregister 的全部 16 variants。本研究不重選最佳 setup，也不使用舊 primary setup selection。
- 期間固定為 2020–2022 `HISTORICAL_DISCOVERY`、2023–2024 `RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`、2025 `STRESS_PREVALENCE_SEEN_NOT_BLIND`。
- `signal_observations.csv`、`measured_move_structures.csv` 與 `confirmed_pivots.csv` 保存 Primary 9 檔逐筆證據；133 檔 secondary universe 完整保留在 bucket、coverage、continuous、decile 與 performance aggregate robustness artifacts，避免在 Git 重複保存超過 100 MB 的 raw rows。

## Causal reconstruction

Primary pivot 為 2-left / 2-right。Pivot 發生後必須等待右側兩個完整 sessions，因此只有 `confirmation_date <= signal_date T` 才可使用。連續同類 pivots 只保留較極端者，同價保留較早 confirmed pivot；截至 T 使用最近完成的 `LOW(A) → HIGH(B) → LOW(C)`。需要 `A < C < B`、兩段各至少一 session 且 `L=B-A>0`，否則不強迫產生 ratio。

`D=C+L`，`R=(Close_T-C)/L`。Primary 固定比較 `R<0.8`、`0.8<=R<1.2`、`R>=1.2`；完整七 buckets 與 3/3 sensitivity 均在正式結果前凍結。3/3 只做 sensitivity，不能替換 primary 2/2。

Entry 僅為 outcome reference：`T+1 regular-session Open`，觀察 Day5/Day10 Close、MFE/MAE 與路徑 hits，net return 使用 repo 正式台股成本。未做最佳 threshold、pivot window、exit、market-cycle、intraday 或 warrant 優化。

## Interpretation limits

這是多重、相關 observations 的歷史研究。Signal-date 與 calendar-month cluster bootstrap 用於呈現抽樣不確定性；setup family、stock 與 market context 結果是 robustness / descriptive diagnostics。即使分類顯示 location edge，也不等於已建立可交易 entry rule，更不是 blind OOS 證據。

Safety invariants：`actual_orders=0`、`actual_fills=0`、`broker_connections=0`、`model_fit_count=0`、`Stage A refit count=0`。

## Run

```bash
python3 -m measured_move_location_incremental_study_v01.main freeze-spec
python3 -m measured_move_location_incremental_study_v01.main publish
python3 -m measured_move_location_incremental_study_v01.main verify
python3 -m unittest discover -s measured_move_location_incremental_study_v01/tests -v
```
