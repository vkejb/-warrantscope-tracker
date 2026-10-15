# MSTR 與 BTC 價格連動性研究（繁中報告）

資料截止：2026-10-14

研究期：2020-01-01～2023-12-31；保留期：2024-01-01～資料截止

範圍：價格關係研究，不含交易策略、門檻調參或下單。

## 先回答五個問題

1. **MSTR 與 BTC 相關性有多高？** 同一美股收盤區間的日對數報酬 Pearson 為 **0.725**、Spearman 為 **0.738**（N=1704）。價格水準的相關性是 0.952，但它受共同趨勢影響，**不能視為預測證據**。
2. **BTC 漲跌 1%，MSTR 平均反應多大？** 全期同步回歸 Beta 為 **1.10**，即 BTC 同一區間變動 1% 時，MSTR 平均同步變動約 1.10%；R²=0.526。這是同步敏感度，不是事前預測。
3. **BTC 下跌時 MSTR 是否更敏感？** BTC 上漲樣本 Beta=1.04，下跌樣本 Beta=0.91；下跌 Beta 未較高。詳細樣本數與門檻事件見 `results/asymmetry.csv`。
4. **BTC 變動是否對 MSTR 有領先資訊？** 休市期間 BTC 到開盤缺口的回歸 R²=0.650、方向一致率=82.63%，顯示資訊會反映在開盤價；但開盤後才可交易時，overnight BTC 對 MSTR open-to-close 的 Pearson=0.046、R²=0.002。前一完整 BTC 美股區間對下一 MSTR 日報酬的 Pearson=-0.051、R²=0.003。相較之下，同期 close-to-close 關係 R²=0.526。因此強證據主要是**同步連動與開盤價格發現**；開盤後/下一日可用的事前資訊偏弱。歷史 MSTR 分鐘資料未取得，5/15/30/60 分鐘測試標為 `INSUFFICIENT_INTRADAY_DATA`，沒有用日線補造。
5. **是否穩定到值得進入下一階段交易策略研究？** 保留期 Pearson=0.784、Beta=1.47、R²=0.615；結論為：**尚不足以僅憑本研究進入策略設計**。高同步相關本身不等於可交易的領先訊號。

## 資料與 point-in-time 對齊

- MSTR/QQQ 來自 Yahoo Finance Chart API；BTC/USD 來自 Bitstamp 15 分鐘 OHLCV。
- 美股日曆採 NYSE schedule，UTC 時戳依 America/New_York DST 對應，並保留提早收盤。
- `btc_open` 只取 candle end `<= market_open` 的完整 K 線；所有 1705 筆可評估列的開盤/收盤 point-in-time 檢查均通過：True。
- MSTR 10:1 split 事件日期為 2024-08-08；原始 OHLC 為 split-adjusted 尺度，事件附近最大絕對 close-to-close 報酬為 9.07%，未出現機械式 -90% 跳變。
- BTC 15 分鐘完整性：來源最新完整 candle 前應有 238020 根、實得 238020 根、內部缺口 0 根；請求終點晚於來源最新完整 candle 的尾端差額為 60 根，不列為資料缺口。開收盤最大 staleness 分別為 0/0 秒。
- 本次 TLS 驗證：False。原始檔 URL、時段、列數及 SHA-256 見 `results/source_manifest.json`。

## 同步連動性

- 全期日報酬 Pearson=0.725、Spearman=0.738、方向一致率=79.46%。
- MSTR 年化波動率=88.82%，BTC=58.38%，波動率比=1.52。
- 20/60/120 日 rolling Pearson/Spearman 與 Beta/R² 分別保存在 `rolling_correlation.csv`、`rolling_beta.csv`。
- BTC 單日超過 ±3%、±5% 的樣本數、MSTR 同向率與平均報酬保存在 `asymmetry.csv`，未因結果更改門檻。

## QQQ 控制與額外解釋力

全期 QQQ-only R²=0.214；加入 BTC 後 R²=0.563，BTC incremental R²=0.348；控制 QQQ 後 BTC Beta=0.98、p=9.77e-108。這支持的是同期額外解釋力，不自動轉化為預測力。

## 年度與環境穩定性

| 年度 | N | Pearson | Beta | R² | 同向率 |
|---:|---:|---:|---:|---:|---:|
| 2020 | 252 | 0.471 | 0.425 | 0.222 | 0.683 |
| 2021 | 252 | 0.731 | 0.945 | 0.535 | 0.821 |
| 2022 | 251 | 0.827 | 1.442 | 0.684 | 0.805 |
| 2023 | 250 | 0.784 | 1.148 | 0.614 | 0.784 |
| 2024 | 252 | 0.745 | 1.433 | 0.555 | 0.798 |
| 2025 | 250 | 0.791 | 1.416 | 0.625 | 0.812 |
| 2026 | 197 | 0.859 | 1.602 | 0.737 | 0.878 |

`regime_comparison.csv` 另列研究/保留期 × BTC 上下跌 × 高低波動環境。高低波動切點只由研究期 60 日 BTC 年化波動中位數決定，再原封不動套用保留期。

固定斷點交互作用檢查列於 `structural_change.csv`：2024-01-01 是預先指定的保留期起點，2024-08-08 是 10:1 split。價格資料可檢出 Beta 是否改變，但**不能把變化歸因於資本結構或估值原因**；要做因果歸因需另接公司融資、可轉債、股本與 NAV premium 資料，超出本輪價格研究範圍。

## 同步相關 vs. 真正預測價值

同步 close-to-close 回歸使用同一時間區間的 BTC 和 MSTR 報酬，回答「一起動多少」，不回答「BTC 是否先知道」。真正的事前測試只有：

1. 上一美股收盤至本次開盤、且 BTC K 線在開盤前已完整結束的 overnight return，分別對開盤缺口及開盤後 open-to-close 報酬；
2. 前一美股 close-to-close BTC 報酬對下一 MSTR 日報酬。

overnight BTC 對開盤缺口即使很強，也可能在可成交前已由 gap 完成價格發現，不能直接當成開盤後 alpha。

這兩者的結果不應被同步高相關掩蓋。盤中 5/15/30/60 分鐘因缺少同期間、可驗證且帶時戳的 MSTR 分鐘資料而不執行，狀態為 `INSUFFICIENT_INTRADAY_DATA`。

## 限制

- Bitstamp 是單一 BTC/USD 現貨場所，不代表全市場 consolidated price。
- 日線 MSTR open/close 來自資料供應商，BTC 是 15 分鐘最後完整 candle；最大 15 分鐘解析度誤差由 staleness 欄位明示。
- 公司行動事件與價格來自同一供應商；已做 split 事件及連續性檢查，但不是交易所官方公司行動主檔。
- 本研究是歷史觀察；沒有前瞻 shadow 驗證、交易成本、可成交性或策略績效。

## 重跑

見 `README.md`。核心指令：

```bash
python3 run_research.py --start 2020-01-01
python3 -m unittest discover -s tests -v
```
