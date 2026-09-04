# 台股現股短線策略 V2.1 固定基準

本目錄忠實實作使用者指定的 V2.1；第一輪不調參、不作網格搜尋、機器學習或事後挑選年份。

個股年度／週 OHLCV 使用專案既有的 [tw-stock-data-release](https://github.com/yukishirotsubasa/tw-stock-data-release/releases/tag/daily-close-csv) 資料包，其上游為 TWSE 與 TPEx 盤後資料；2021 僅供 20 日指標暖機，正式統計從 2022 開始。

## 重跑順序

1. 下載 TAIEX：

```bash
python3 download_official.py taiex --output /private/tmp/v21/taiex.csv
```

並下載證券主檔，供金融股排除使用：

```bash
python3 download_official.py stock-info --output /private/tmp/v21/stock_info.csv
```

既有 OHLCV 資料包在 2023-05-25、2025-02-06、2026-05-28 缺少整批上市行情；本次基準用證交所官方日資料補回：

```bash
python3 download_official.py twse-prices \
  --output /private/tmp/v21/twse_price_supplement.csv \
  --dates 20230525 20250206 20260528
```

2. 先跑價量條件並建立法人資料需求清單：

```bash
python3 main.py /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip /path/weekly_2026_W*.zip /private/tmp/v21/twse_price_supplement.csv \
  --taiex /private/tmp/v21/taiex.csv --stock-info /private/tmp/v21/stock_info.csv \
  --output-dir output --prepare-only
```

3. 下載候選股所需的外資／投信資料。先用櫃買中心官方歷史端點補上櫃股票，再用 FinMind 逐股端點補其餘缺口；兩者都有可續跑檢查點：

```bash
python3 download_official.py tpex --needs output/institutional_needs.json \
  --output /private/tmp/v21/institutional.csv \
  --checkpoint /private/tmp/v21/tpex_checkpoint.json --workers 2
```

```bash
python3 download_official.py finmind --needs output/institutional_needs.json \
  --output /private/tmp/v21/institutional.csv \
  --checkpoint /private/tmp/v21/finmind_checkpoint.json --workers 2
```

4. 正式回測：

```bash
python3 main.py /path/yearly_2021.zip /path/yearly_2022.zip /path/yearly_2023.zip /path/yearly_2024.zip /path/yearly_2025.zip /path/weekly_2026_W*.zip /private/tmp/v21/twse_price_supplement.csv \
  --taiex /private/tmp/v21/taiex.csv --stock-info /private/tmp/v21/stock_info.csv \
  --institutional /private/tmp/v21/institutional.csv \
  --output-dir output
```

正式執行會輸出 `trades.csv`、`signal_events.csv`、`yearly_summary.csv`、`breakout_type_summary.csv`、`exit_reason_summary.csv`、`backtest_summary.json`、`backtest_report.md`、`data_audit.json` 與 `validation_summary.json`。若逐筆重算驗證失敗，程式會停止，不會留下看似成功的結果。

## 固定成本假設

- 每筆名目金額 30,000 元，對應使用者的小本金基準；僅用來計算零股最低手續費與股數取整的實際比例，不代表能同時承接所有訊號。
- 手續費 0.1425%、電子折扣 0.28、每邊最低 1 元。
- 股票賣出證交稅 0.3%。
- 同時輸出零滑價與單邊 0.1% 滑價。

所有參數集中在 `config.py`。策略採獨立交易統計，因原始規格沒有定義同時持股上限、資金分配或訊號競爭規則，所以不虛構投資組合權益曲線。

## 已知資料限制

- OHLCV 來自 TWSE／TPEx 年度與週資料包；一般市場 Open 不等於歷史零股第一筆成交價。
- 法人資料以上櫃中心官方歷史日表為優先，其餘由 FinMind 的 `TaiwanStockInstitutionalInvestorsBuySell` 逐股歷史端點補齊；欄位保留 `Foreign_Investor` 與 `Investment_Trust`，重疊樣本無差異，且不以 0 假補缺值。
- 沒有完整的 point-in-time 處置股、全額交割股歷史，不能用今日狀態回填，因此這兩項排除未執行。
- 金融股以 FinMind 具日期的證券主檔產業標籤辨識，能涵蓋 60xx 證券／期貨股；但該主檔不是完整逐日產業快照，仍有 point-in-time 分類限制。
- 四碼普通股歷史行情包含退市股票，可降低只保留現存股票造成的 survivorship bias；但缺少永久證券 ID，代碼重用仍可能造成偏誤。
- 公司行動沒有正式調整因子，V2.1依原始未還原OHLCV執行；短持有期仍可能跨越除權息或減資事件。
- 持有日按 TAIEX 交易日計數；若個股中途停牌，停牌日仍會推進第 5／8 日時鐘，但當日沒有 Close 可判斷條件。若必要的 T+1／D+1 Open 或第 8 日 Close 不存在，交易會取消或設限，不會延後拿其他日期冒充。
