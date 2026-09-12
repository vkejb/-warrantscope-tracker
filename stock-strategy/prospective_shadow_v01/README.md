# Prospective N Compact Shadow V0.1

`prospective_shadow_v01` 是從 2026-09-07 起使用的不可回寫觀察器。它只讀本機既有日線 OHLCV，盤後掃描已凍結的 `N_RETEST`，再以既有共用函式標記：

```text
N_COMPACT_RETEST = pivot_separation_sessions <= 7 and bottom_difference > 0
```

本模組是 `SHADOW_ONLY_NOT_SUBMITTED`。沒有券商 SDK、元大 API、帳戶、憑證、網路下載、委託或成交功能；`is_actual_order` 與 `is_actual_fill` 永遠為 `false`。未來若資料來源改成元大，只能在 `MarketDataProvider` 讀取介面後另建 provider，不能把交易方法接進本模組。

## 不可變研究契約

- 訊號只能讀取 T 日及以前資料。Provider 在建構 `PreparedStock` 前先物理裁切 `date <= T`。
- Detector 直接呼叫 `reversal_event_study_v01.study.build_pattern_observation`，不複製或改寫 N_RETEST。
- Compact 標記直接呼叫 `multi_setup_study_v01.setup_detectors.is_compact_retest`，不另設門檻。
- N_RETEST 的每檔 10 市場日冷卻期會從可用歷史完整重播；accepted 非 Compact N 也會影響冷卻期。
- 凍結 config fingerprint 或兩個共用 detector 原始碼雜湊漂移時，掃描會 fail closed。規則若有正式新版，應另建新版本模組，不能改這個資料庫的定義。
- CLI 不提供 threshold、optimization、backfill 或任意 signal date 參數。正式 `run-daily` 只可在台北當日 13:35 後執行。

## 資料與 append-only 保證

資料目錄預設為 `prospective_shadow_v01/data/`：

- `prospective_signals.csv`：不可變 signal snapshot。唯一鍵為 `(stock_id, signal_date, setup)`。
- `prospective_outcomes.csv`：future outcome 的 append-only revision；不會更新或覆寫 signal 列。
- `prospective_scan_log.csv`：每天一筆封存，包括零訊號日，保存來源雜湊、數量及 signal-set digest。
- `shadow_status.json`：可由三條 ledger 重建的狀態快取，不是歷史真相來源。
- `runs/<run_id>/run_manifest.json`：每次有效資料執行的不可變執行紀錄；此 runtime 目錄預設不納入 Git。

每條 CSV 都有 `previous_record_hash` / `record_hash` 雜湊鏈，寫入時使用 process lock、暫存檔、`fsync` 與原子 replace。同日同內容重跑不新增資料；同鍵不同內容、已封存日期的來源或結果改變、歷史 signal 倒序補寫、outcome 已知值被改動，都會拒絕整批操作。`shadow_status.json` 可以更新，但 signal 歷史列不會更新。

Signal snapshot 保存 pivot 日期與 Close 價格（`*_pivot_price` 與明確的 `*_pivot_close` 同值）、所有指定 geometry、signal features、volume features、資料來源與凍結規則雜湊。它不含任何 entry、forward return、MFE 或 MAE 欄位。

Outcome 在資料成熟時單調補入：

- T+1 Open proxy 與 entry gap；
- Day 1 / 3 / 5 / 10 Close return；
- Close-based MFE / MAE 5D、10D；
- Close-based `+8% before -5% within 10 trading days`、首次 barrier 日與 path result。

五日與十日 MFE/MAE 只會在完整五或十個市場日可用後寫入，不會拿不完整視窗冒充。完整 Day 10 結果會逐欄核對 `multi_setup_study_v01.outcomes.evaluate_unified_outcome`。

## 本機資料要求

每次資料執行需要：

1. 既有 OHLCV ZIP（可加本機 supplement CSV）；
2. 獨立、point-in-time、升冪且不重複的 `trading_calendar.csv`，欄名為 `date`。

Provider 不下載資料，並拒絕壞檔、invalid row、任何重複 code-date、缺少市場日、目標日不存在、目標日 coverage 明顯不足，以及讀檔期間來源變動。時鐘到 13:35 只代表允許開始檢查，不代表資料已完整；是否封存仍由上述 data-readiness 檢查決定。若當日資料缺失，該日不會被誤記成零訊號。

## 使用方式

從 `stock-strategy/` 執行：

```bash
python3 -m prospective_shadow_v01.main init

python3 -m prospective_shadow_v01.main run-daily \
  --archives /path/to/existing_daily_ohlcv.zip \
  --supplements /path/to/optional_latest.csv \
  --trading-calendar /path/to/trading_calendar.csv

python3 -m prospective_shadow_v01.main update-outcomes \
  --as-of 2026-09-08 \
  --archives /path/to/existing_daily_ohlcv.zip \
  --supplements /path/to/optional_latest.csv \
  --trading-calendar /path/to/trading_calendar.csv

python3 -m prospective_shadow_v01.main status

python3 -B -m prospective_shadow_v01.main diagnostics --date 20260909
```

若沒有 supplement，可完全省略 `--supplements`。本版不建立排程器，因 repository 尚未有一條可證明每日盤後已完整落地的行情更新流程；資料供應流程確定後，再讓外部排程每天呼叫同一個 `run-daily` 即可。

`diagnostics` 只允許重播已有一筆 `COMPLETE` scan 的日期。它從該日
readiness audit 取回當時封存的 archive 清單，並強制核對 trading calendar、
input manifest、prospective config、規則及所有 scan counts。輸出包含 raw／
accepted N_RETEST、N Compact 與每檔未通過 Compact 的逐條原因。此命令不建立
`ShadowStore`，不取得 write lock、不 seal、不寫 ledger/status/run manifest，也沒有
下單或 broker 入口；任何封存資料不一致都會 fail closed。

## 驗證

```bash
cd stock-strategy
python3 -B -m unittest discover -s prospective_shadow_v01/tests -v
```

測試固定檢查：T+1 後資料改動不影響 signal、重跑不重複、conflict 不改檔、outcome updater 不修改 signal bytes、outcome 只允許單調 revision、Compact 邊界與共用函式一致、diagnostics 前後 ledger/status bytes 完全相同，以及 production package 沒有券商／網路／憑證／下單入口。

這些 observation 只能用來做 2026-09-06 之後的 prospective Shadow confirmation；不得依其 outcome 回頭修改本版規則，也不是實際可成交績效。
