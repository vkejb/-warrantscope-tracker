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
```
