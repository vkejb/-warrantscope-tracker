# 台股零股策略研究室

這是一個與 WarrantScope 完全分開的現股選股／回測工具，不讀取 `data.js`、權證 Raw、主力排行或 Episode。

## 策略

- 股票池：歷史當時存在的四碼普通股（代號首碼 1–9），排除 ETF、權證及特殊商品。
- 流動性：股價至少 15 元，20 日平均成交金額至少 3,000 萬元。
- 趨勢：收盤高於 120 日均線，20 日動能為正。
- 量能：最近 5 日均量至少為 20 日均量的 1.2 倍。
- 排序：`0.7 × 120 日動能 + 0.3 × 20 日動能`，取前 5 名。
- 執行：月底收盤產生訊號，下一交易日開盤換股，三萬元等權零股配置。
- 成本：手續費率 0.1425%、28 折、每筆最低 1 元；賣出交易稅 0.3%。

## 資料與限制

年度 CSV 由 [tw-stock-data-release](https://github.com/yukishirotsubasa/tw-stock-data-release/releases/tag/daily-close-csv) 提供，原始來源為 TWSE `MI_INDEX` 與 TPEX 每日收盤行情。2019 只作 120 日指標暖機，回測期間為 2020–2025。

回測使用未還原的官方 OHLCV；程式將單日價格比例小於 0.55 或大於 1.80 視為分割／併股並維持報酬序列連續。結果不含現金股利，因此不是完整總報酬。這是研究模型，不是獲利保證。

## 重跑

```bash
python3 scripts/run_backtest.py /path/yearly_2019.zip /path/yearly_2020.zip /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip --output backtest-result.json
python3 scripts/run_backtest_v2.py /path/yearly_2019.zip /path/yearly_2020.zip /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip --output backtest-result-v2.json
python3 scripts/research_event_driven.py /path/yearly_2019.zip /path/yearly_2020.zip /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip --output research-event-driven.json
python3 scripts/research_daily_strength.py /path/yearly_2019.zip /path/yearly_2020.zip /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip --output research-daily-strength.json
python3 scripts/research_short_surge.py /path/yearly_2019.zip /path/yearly_2020.zip /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip --output research-short-surge.json
```

V2 在執行前固定加入 0050 多頭濾網、6-to-1 月動能、波動上限、三檔持股及排名緩衝以降低換手。2020–2022 為設計期、2023–2024 為驗證期、2025 為鎖定規則後的樣本外測試。V2 仍未通過，結果保留而不以 2025 重新調參。

後續歸因研究一次固定測試五個經濟邏輯不同的版本，不作參數網格搜尋。最佳版本是「低波動趨勢10檔／季換股＋大盤濾網」：總報酬 +23.97%、2024驗證 +9.37%、2025壓力測試 -9.88%，仍顯著落後0050。研究結果指出增加分散、降低換手與保留大盤濾網是有效方向，但純價量選股不適合取代ETF核心。

事件驅動研究將「找新股票」與「賣出持股」分離：每月只更新候選名單及補足空缺，原持股不因月底到期而賣出；只有大盤轉空、固定停損、趨勢破壞或獲利後移動停利才在下一交易日開盤退出。三組退出規則在執行前固定。100日趨勢加10%移動停利的總報酬為 +19.61%、最大回撤 20.31%，2024 +7.04%、2025 +16.20%；顯示事件退出可改善回撤及續抱，但尚未形成超越0050的獨立策略。

每日強勢研究使用每日收盤橫斷面排名並於隔日開盤交易。只補空缺版本總報酬 +9.54%，跌出30名才替換為 -37.54%，每日機械持有前10名為 -54.46%。零成本敏感度顯示三者分別約 +39.59%、-0.41%、+25.94%，證明高頻排名的毛優勢不足以負擔真實交易摩擦。首次結果後追加的連續3日／5日確認僅屬探索性診斷，不得用來宣稱樣本外最佳；兩者2024均為負報酬，未通過驗證。

短線急漲研究把成功明確定義為隔日開盤進場後，10個市場交易日內先達 +8%、否則以 -5% 停損；同一根日K同時觸及兩者時保守視為停損。三個事前版本全部失敗：價量突破 -78.99%、連續3日轉強 -77.73%、過熱排除 -62.50%。三者即使假設零交易成本仍分別為 -43.77%、-34.61%、-19.22%，表示失敗主因不只是費用，而是突破後追價缺乏正期望。首次失敗後追加的量縮盤整突破為探索性診斷，結果仍為 -57.15%，不可用來選模。

短線結果只能視為一般整股日OHLC的理論回測：一般市場開盤不等於歷史零股首筆成交，沒有模擬漲跌停排隊；公司行動只採價格跳變啟發式正規化。程式對日K同時觸及停損／停利採停損優先、停牌期間持有期限照市場日累計、未成交退出意圖持續保留，期末無報價則採零清算值。
