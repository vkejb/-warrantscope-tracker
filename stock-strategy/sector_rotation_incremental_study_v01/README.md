# SECTOR_ROTATION_INCREMENTAL_STUDY_V0_1

本模組只研究 Frozen `LINEAR_RIDGE_MFE10` Stage A Top30 內，族群環境是否能改善後續 path quality；不是新的全市場選股器，不連券商、不下單。

## Frozen contract

- Stage A score、每日 Top30、outcome 與成本直接讀取已發布 immutable store；refit count 固定為 0。
- 2020–2022 是 historical discovery；2023–2024 是 retrospective confirmation, not blind OOS；2025 是 stress/prevalence-seen, not blind。
- 所有 discovery percentile boundaries 只由 2020–2022 產生，之後 frozen apply。

## Phase 0

Repository 沒有可證明 2020–2025 每日 membership 與正式分類變更日的官方產業歷史。既有 `v21` 稽核亦明確說明現有標籤是 latest/historical snapshots，而非完整 PIT daily history。因此 `OFFICIAL_SECTOR` 標記為 `OFFICIAL_SECTOR_PIT_UNSAFE`，不使用今天分類倒灌歷史，也不產生 `official_sector_results.csv`。

`DYNAMIC_MARKET_PEERS` 使用每個 T 日及以前最近 60 個市場交易日報酬，在當日 eligible universe 中選 Pearson correlation 最高的 10 檔；至少 40 個共同 session，排除自己，同分依 stock id。所有 aggregate 皆為 leave-one-out。

Turnover 類欄位只稱 `capital_activity_proxy` / `turnover_concentration`，計算為 `close * volume`，不聲稱資金淨流入。

## Composite discipline

只有 discovery 內至少兩個不同 economic families 的極端 quintile 同時呈現較高 success、較低 downside-first，才建立簡單 score。每 family 最多一個 feature；HIGH 使用 frozen Q60，LOW 使用 frozen Q40。Top15/10/5 都是預註冊 diagnostic，不能看 later period 後挑 K。

## 執行

正式輸出不可覆寫：

```bash
cd stock-strategy
PYTHONPATH=. python3 -B -m sector_rotation_incremental_study_v01.main publish
```

大型 observation-level peer store 留在 ignored `runtime/`。正式摘要均由 `run_manifest.json` 記錄 hash 與 provenance。
