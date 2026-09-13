# DERIVATIVES_CONTEXT_GATE_STUDY_V0_1

本模組只研究市場層級衍生品環境是否能改善已發布、完全 frozen 的 Stage A Top30 之 T+1 進場品質。它不修改選股、不重訓 Stage A／Conditional Path、不建立 prospective shadow，也不連券商或下單。

## 預註冊資料與 PIT 契約

- TX 全契約未沖銷量：[TAIFEX 官方年度期貨每日行情](https://www.taifex.com.tw/cht/3/dlFutDailyMarketView) ZIP，只取一般交易時段。近月 rollover parser 仍保留供 audit，但正式 basis family 因 TWSE TAIEX 月端點無法形成完整、可重現的 2020–2025 archive 而 fail closed，不進入 bucket/gate。
- TXO P/C：由 [TAIFEX 官方年度選擇權每日行情](https://www.taifex.com.tw/cht/3/dlOptDailyMarketView) ZIP 的一般交易時段 TXO 全到期契約加總，依官方表口徑合併週與月契約。
- 上述均為 T 日收盤後資料，只能判斷 T+1 Open 之後的 outcome；不作同一交易時段預測。
- 外資 TX 未平倉免費官方查詢不覆蓋完整 2020–2022 discovery，因此 fail closed 為 `NOT_TESTED_DATA_UNAVAILABLE`，未以第三方或現況快照補洞。

Threshold 僅由 2020–2022 的 20/40/50/60/80 百分位建立，2023–2025 不重新估計。若 discovery 至少兩個 economic families 同時呈現高 downside-first 與更差 MAE，才建立每條件 +1 的簡單 score；LOW=0、MEDIUM=1、HIGH>=2。統計推論以 signal-date 與 calendar-month cluster bootstrap 為主，避免把同日 30 檔視為獨立市場樣本。

## 執行

```bash
cd stock-strategy
PYTHONPATH=. /Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m pytest derivatives_context_gate_study_v01/tests
PYTHONPATH=. /Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -m derivatives_context_gate_study_v01.main publish
```

`publish` 是 fail-closed、單次正式發布；既有正式 artifact 存在時拒絕覆寫。大型官方 raw/cache 保留在忽略版控的 `runtime/`，正式摘要保存來源與 artifact SHA-256。
