# V2.1 Diagnostic Analysis 重跑說明

這個目錄只存放 V2.1 固定基準的描述性診斷結果。診斷程式不重建訊號、不修改交易規則、不調參，也不會寫入 `v21/output/`。

從專案根目錄執行：

```bash
python3 stock-strategy/v21/diagnostic_analysis.py \
  /private/tmp/yearly_2021.zip \
  /private/tmp/yearly_2022.zip \
  /private/tmp/yearly_2023.zip \
  /private/tmp/yearly_2024.zip \
  /private/tmp/yearly_2025.zip \
  /private/tmp/weekly_2026_W*.zip \
  /private/tmp/v21/twse_price_supplement.csv \
  --taiex /private/tmp/v21/taiex.csv \
  --stock-info /private/tmp/v21/stock_info.csv \
  --trades stock-strategy/v21/output/trades.csv \
  --signal-events stock-strategy/v21/output/signal_events.csv \
  --output-dir stock-strategy/v21/diagnostic \
  --bootstrap-reps 5000
```

程式會先確認原 V2.1 `validation_summary.json` 已通過，記錄 baseline 輸出檔 SHA-256，完成後再逐檔比對，若任何 baseline 檔案被改動就停止。

口徑：

- Forward Day 1 是 T+1 Open 進場當日的 Close；Day 3/5/8 依 TAIEX 交易日推進。
- 缺少目標日個股 Close 時保留空值，不以前後日期代替。
- 反事實 Day 8 僅為研究欄位，不改寫原交易。
- Feature quintile 採近等樣本數排序；同值以股票代碼與訊號日固定排序拆分。
- Bootstrap 以 signal date 與 month 分群抽樣，不使用單筆交易 IID bootstrap。
- 現有產業主檔缺少足夠歷史 as-of 覆蓋，因此不回填未來分類，只輸出日期集中度。
