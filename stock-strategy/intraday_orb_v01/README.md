# 台股盤中零股研究基準 `INTRADAY_ORB_V0_1`

這是與 `v21/` 完全分離的盤中研究程式。第一版只做固定條件重播、進場訊號 forward return 與零股 shadow quote 檢查；不做參數最佳化，也沒有任何正式下單入口。

## 安全邊界

- 本套件不匯入元大交易元件，不讀取帳號、密碼或憑證。
- 不存在 live／production 命令或切換開關。
- 所有零股結果永久標示為 `NOT_SUBMITTED_SHADOW`、`is_actual_order=false`、`is_actual_fill=false`。
- 最佳賣價及揭示量只能代表可能可執行，不能證明排隊成交。
- 資料缺漏、分鐘不完整或歷史不足時停止產生該股票訊號，不向未來補值。
- 每日唯一選股要求當日 Top 30 全部可完整判斷；任一成員在必要區間被資料缺漏 censor，該日不產生 selected signal。

## 固定規則

### T-1 盤前股票池

只接受上市／上櫃四碼普通股且 T-1 狀態正常，並要求最近 60 個市場交易日資料完整：

- T-1 收盤至少 15 元。
- 最近 20 日每日成交股數中位數至少 100 萬股。
- 最近 20 日每日成交金額中位數至少新台幣 1 億元。
- `Close(T-1) > SMA20(T-1) > SMA60(T-1)`。
- 5 日與 20 日個股簡單報酬都高於相同期間的對應市場指數報酬。
- T-1 收盤至少為最近 60 日最高收盤的 95%。
- 依 `RS20 降冪、RS5 降冪、20日成交金額中位數降冪、股票代碼升冪`，每日固定取前 30 檔。

上市股票使用 canonical `TAIEX`，上櫃股票使用 canonical `TPEX`。資料收集器必須把供應商代碼映射為這兩個內部代碼；日線與分鐘線不接受其他或空白的 `index_id`。所有盤前欄位都必須是當時已知的 point-in-time 資料。

固定 V0.1 是上市＋上櫃合併選股，因此 `daily`、`index_daily` 與交易日曆都必須同時涵蓋 TWSE、TPEX。只提供單一市場會直接拒絕，不能仍宣稱是完整 V0.1。

### 盤中訊號

輸入只使用普通交易的一分鐘 K；`bar_end=09:01` 代表已經完成的 `[09:00, 09:01)`：

1. 以 `bar_end 09:01～09:15` 的 15 根完整 K 建立 `OR_high`、`OR_low`。
2. 第一次評估為 09:17，最後一次為 11:00。
3. 最新兩根完整一分鐘 K 的收盤都必須高於 `OR_high`，且最新收盤至少高出一個台股普通股合法升降單位。
4. 截至該分鐘的精確 `VWAP = 累積成交金額 / 累積成交股數`；收盤須高於 VWAP，但不得高出超過 2%。
5. 收盤不得高出 `OR_high` 超過 1%。
6. `RVOL = 今日同分鐘累積量 / 前20個完整交易日相同分鐘累積量中位數`，至少為 1.8。
7. 個股相對前收上漲 1%～6%，且比同時間對應市場指數至少強 1%。
8. 確認 K 必須有成交量，並距漲停價至少兩個合法升降單位。
9. 每檔每天只留第一個完整訊號。每日先選最早觸發分鐘；同分鐘依盤中 RS、RVOL、T-1 成交金額中位數及股票代碼決定唯一一檔。

### 零股 shadow quote

- 報價交易所時間必須嚴格晚於訊號，接收延遲不超過 3 秒，且在訊號後 60 秒內。
- Feed 與市場狀態均須正常。
- `(ask1-bid1)/midpoint <= 0.5%`。
- `ask1/regular_last-1 <= 0.3%`。
- 前兩檔委賣量至少為研究股數的兩倍。
- `ask1` 不得高於訊號價 0.3%，且距漲停至少兩檔。
- 三萬元研究本金最多使用 95%，單筆股數上限 999 股。

通過上述條件仍不記為成交，因歷史五檔無法還原自身委託的排隊順位。

## CSV 輸入格式

所有時間採 Asia/Taipei。價格單位為元、數量單位為股、成交金額單位為元。欄位不可用推估值替代。

### `trading_calendar.csv`

```text
date,market
```

交易日曆必須包含研究目標日與之前至少 60 個已知交易日。它只描述預先公布的交易日，不含價格，讓 T 日股票池在盤前只需使用到 T−1 的日線資料。

### `daily.csv`

```text
date,symbol,name,market,security_type,trading_status,close,volume,turnover
```

- `market`：`TWSE` 或 `TPEX`
- `security_type`：符合策略者須為 `COMMON_STOCK`
- `trading_status`：符合策略者須為 `NORMAL`

### `index_daily.csv`

```text
date,market,index_id,close
```

每個市場日期只能有一列對應指數。

### `minutes.csv`

```text
bar_end,symbol,market,open,high,low,close,volume,turnover,limit_up
```

分鐘資料必須包含零成交分鐘並延續前價；`volume=0`、`turnover=0`。這使程式能區分真正零成交與資料遺失。

### `index_minutes.csv`

```text
bar_end,market,index_id,close
```

### 選用的 `odd_quotes.csv`

```text
exchange_time,received_time,symbol,market,bid1,ask1,ask1_quantity,ask2_quantity,regular_last,limit_up,feed_state,market_status
```

## 使用方式

先只產生每日 Top 30 與分鐘資料需求：

```bash
cd stock-strategy
python3 -B intraday_orb_v01/main.py prepare \
  --daily /path/daily.csv \
  --index-daily /path/index_daily.csv \
  --trading-calendar /path/trading_calendar.csv \
  --start-date 2020-01-01 --end-date 2025-12-31 \
  --output-dir intraday_orb_v01/output/prepare-run-001
```

有完整分鐘資料後重播訊號：

```bash
python3 -B intraday_orb_v01/main.py research \
  --daily /path/daily.csv \
  --index-daily /path/index_daily.csv \
  --trading-calendar /path/trading_calendar.csv \
  --minutes /path/minutes.csv \
  --index-minutes /path/index_minutes.csv \
  --start-date 2020-01-01 --end-date 2025-12-31 \
  --output-dir intraday_orb_v01/output/research-run-001
```

若另有合法取得的歷史零股五檔，可加上：

```text
--odd-quotes /path/odd_quotes.csv
```

## 輸出

每次必須指定一個尚不存在的新目錄，程式不會覆寫或混用舊結果。只有包含 `run_manifest.json` 且狀態為 `COMPLETE` 的目錄才是完整一次執行。

- `config_snapshot.json`：固定參數與 SHA-256 fingerprint。
- `universe.csv`、`minute_needs.csv`：盤前股票池及資料需求。
- `signal_evaluations.csv`：每分鐘所有條件真假與拒絕原因；重播時逐列寫出，避免多年資料把明細全部留在記憶體。
- `raw_signals.csv`、`selected_signals.csv`：個股原始訊號與每日唯一選擇。
- `forward_returns.csv`：5／15／30／60 分鐘與收盤 forward return、MFE、MAE。
- `forward_summary.csv`、`bootstrap_results.csv`：不同總摩擦情境與日期 cluster bootstrap。
- `shadow_quote_checks.csv`、`shadow_outcomes.csv`：零股報價檢查；不是成交紀錄。
- `data_audit.json`、`validation_summary.json`、`research_report.md`：資料與結果稽核。
- `run_manifest.json`：最後才寫入的完整執行標記與本輪產物清單。

Forward return 以第二根確認 K 的收盤為研究基準，MFE／MAE只使用訊號成立後的完整 K。尚未定義固定出場，因此不能由這些輸出宣稱完整策略勝率、權益曲線或可實現損益。

## 目前資料限制

專案現有歷史資料是日線，缺少一分鐘成交金額、對應市場指數分鐘線與歷史零股五檔，所以現在可以完成並驗證程式，但不能製造 2020～2025 的盤中績效數字。元大權限開通後，行情收集器應放在獨立程序，只輸出上述標準化 CSV／事件資料給本研究套件；交易元件不得匯入本套件。

輸入日線還必須是當時實際可投資的完整歷史股票母體，包含後來下市、當時新上市與市場轉換的股票，不能只拿今天仍存在的股票清單回填歷史。程式能驗證收到的列與市場覆蓋，無法自行證明外部資料源沒有 survivorship bias；這一點必須在正式研究資料的來源稽核中確認。

## 測試

```bash
cd stock-strategy
python3 -B -m unittest -v intraday_orb_v01.test_intraday_orb_v01
```
