# Shadow Daily Runner V0.1

`shadow_daily_runner` 是 `prospective_shadow_v01` 外部的資料準備與 macOS
`launchd` 執行層。它不修改 N_RETEST、N Compact 規則、threshold、detector、
readiness 或 ledger 定義，也沒有元大 API、券商連線、委託或成交功能。

## 資料來源與相容 schema

歷史 `weekly_2026_W01`～`W35` 沿用既有
[`tw-stock-data-release`](https://github.com/yukishirotsubasa/tw-stock-data-release)
週檔。該專案標示原始來源為 TWSE 與 TPEx，schema 固定為：

```text
date,code,name,volume,open,high,low,close
```

W36 起由 runner 直接取得：

- TWSE 官方 `MI_INDEX`（`ALLBUT0999`）；
- TPEx 官方「上櫃股票每日收盤行情（不含定價）」Big5 CSV；
- TWSE 官方年度開休市日程。

直接官方層沿用 W01～W35 上游的既有 universe compatibility filter：
`0050` 或四碼代號，且名稱不以 `N` / `DR` / `R1` / `R2` / `特` /
`售xx` / `購xx` 結尾。這是為了不在 W36 後悄悄擴張 frozen research
universe；它是舊資料生產器的相容規則，不是完整、權威的 point-in-time
security master（公司名稱剛好以「特」結尾也會被舊規則排除）。

Raw response 以 SHA-256 content address 保存；clean ZIP 固定 member timestamp、排序與
內容，檔名同時含 source-manifest hash 與 clean hash。若同名 immutable 檔內容不同會
fail closed。

`trading_calendar.csv` 只有 `date` 欄、ISO `YYYY-MM-DD`、升冪且 unique；旁邊的
`trading_calendar.metadata.json` 保存官方 URL、retrieval timestamp、raw source hash、
derived calendar hash 與推導方法。年度行事曆之外，只接受標題格式完全吻合的
TWSE 臨時休市公告；已驗證的 raw/hash/record evidence 會與最新新聞清單做
conflict-fatal union，避免新聞 API 日後滾動移除舊公告時把休市日誤加回。

## 缺價 normalization

所有 eligible row 在進入 tradable archive 前分類：

- `blank_ohlc_zero_or_no_volume`：保留 audit，從 tradable bar 排除；
- `blank_ohlc_positive_volume`：必須以同日、同代號、同 volume 的官方 regular-session
  close source 再確認四價仍全空，才可標為 non-tradable exclusion；否則 fail closed；
- `partial_ohlc_missing`：fail closed；
- `parse_or_schema_error`：fail closed。

沒有 forward-fill，也沒有無稽核的 `dropna()`。每列 exclusion reason 與官方來源 hash
保存在 `runtime/audit/`。

## 執行流程

從 `stock-strategy/` 執行：

```bash
python3 -B -m shadow_daily_runner.main prepare
python3 -B -m shadow_daily_runner.main preflight-latest
python3 -B -m shadow_daily_runner.main attempt
python3 -B -m shadow_daily_runner.main status
```

`prepare` 可在白天先建立／檢查資料，不會寫任何 prospective ledger。`attempt` 沒有
日期參數，只接受台北當日 14:25～16:05；2026-09-07 或更早一律拒絕，避免 backfill。
同日成功後的 retry 為 no-op。

## macOS 系統通知

runner 以 macOS 內建的 `/usr/bin/osascript` 發送 Notification Center 通知，沒有
Homebrew、`terminal-notifier`、第三方 App 或外部 push service 相依。通知是獨立的
best-effort 輸出層：任何通知、狀態或 notification log 錯誤都不會改變 shadow run
結果，不會修改 prospective ledger，也不會阻止 seal。

- seal、ledger hash 與 prospective status 全部驗證成功後才通知；
- 有 N Compact 訊號時顯示檔數與最多前 5 檔（既有資料有名稱才顯示名稱）；
- N Compact 為 0 檔仍會通知，表示當日正常完成並封存；
- 14:30、15:00、15:30 readiness fail 不通知；只有設定中最後一個 16:00 attempt
  確認仍失敗才通知；
- final failure 若是 historical preflight fail，會顯示獨立的 Historical preflight
  訊息，不包含 stack trace；
- 同一 `signal_date + notification_type + payload_hash` 成功送出後不會重複通知；
  若通知本身失敗，之後的同狀態 retry 仍可再嘗試。

通知狀態保存在
`shadow_daily_runner/runtime/notifications/notification_state.json`，通知紀錄保存在
`shadow_daily_runner/runtime/logs/notification.log`。整個 `runtime/` 都不納入 Git。
`status` 指令會以 additive `notification` 區塊顯示最近一次通知狀態，不改變既有
runner 或 prospective status schema。

只測試系統通知、不執行 strategy、不下載行情、不建立 scan 且不 seal 日期：

```bash
python3 -B -m shadow_daily_runner.main test-notification
```

成功會回報 `NOTIFICATION_TEST_SENT`；失敗會回報
`NOTIFICATION_TEST_FAILED` 與簡短診斷。若指令回報成功但畫面沒有出現通知，請至
macOS「系統設定 → 通知」檢查 Terminal、`osascript` 或實際執行來源的通知權限。
從 Terminal 手動執行與 `launchd` 背景執行屬於不同環境，兩者權限可能不同；請先用
上面的 CLI 測試 Terminal，再讓已安裝的 LaunchAgent 執行一次並查看
`runtime/logs/notification.log`。不需要、也不應為通知功能修改 plist 排程。

Readiness 同時要求 calendar target、完整 session、0050、TWSE/TPEx 個別 coverage、
total coverage、zero duplicate、zero unresolved invalid、固定 source hash，最後還必須讓
既有 `ExistingDailyDataProvider.load_through()` 自己完整通過。失敗只寫 runner audit／
attempt report，不呼叫 `run-daily`，因此不會把資料缺口寫成 zero-signal day。

### Historical preflight health check

每次 `attempt` 在任何 `prospective_shadow_v01` 呼叫之前，都會對 `data_start` 至台北
當日重新做全量 historical preflight。它不是策略 detector，也不會讀寫 signal／outcome
ledger。固定檢查：

- `trading_calendar.csv` 每一個 session 都有 archive price rows；
- 每日 TWSE 與 TPEx 官方來源都存在，且各自有正的 eligible tradable row count；
- 每日 TWSE、TPEx、combined count 不低於全期間中位數的既有 70% data-coverage floor；
- clean archive 的每日筆數與官方雙市場 source manifest 一致；
- duplicate code-date 與 unresolved invalid tradable OHLC 都是 0；
- calendar、官方 raw source、clean archive 與 source-manifest hash 均完整且吻合。

輸入 contract 為 `runtime/audit/historical_source_coverage.json`。`sessions` 必須依日期排序，
每個 calendar session 至少包含：

```json
{
  "date": "2026-09-08",
  "status": "READY",
  "TWSE": {
    "status": "READY",
    "row_count": 1076,
    "request_url": "https://www.twse.com.tw/...",
    "retrieved_at_utc": "2026-09-08T08:00:00Z",
    "sha256": "<64 hex>",
    "path": "/immutable/raw/twse_eod_20260908/<sha256>.json",
    "response_date": "2026-09-08"
  },
  "TPEX": {
    "status": "READY",
    "row_count": 859,
    "request_url": "https://www.tpex.org.tw/...",
    "retrieved_at_utc": "2026-09-08T08:00:00Z",
    "sha256": "<64 hex>",
    "path": "/immutable/raw/tpex_eod_20260908/<sha256>.csv",
    "response_date": "2026-09-08"
  },
  "combined_count": 1935
}
```

Manifest 頂層另須有 `schema_version: "1"`、`trading_calendar.sha256` 與 `sessions`。
W36 起的 direct official audit 會安全地延伸此 manifest；W01～W35 必須先由一次性的官方
歷史修復工程建立，不得從股票代號猜測市場。缺 manifest、缺任一日／任一市場、來源 URL
非官方、hash 不合法、count 不一致或低 coverage，結果一律為 `FAIL_CLOSED`。完整結果寫到
`runtime/audit/historical_health_audit.json`。

也可在完全不下載資料、不觸發 daily scan 的情況下手動稽核已準備好的輸入：

```bash
python3 -B -m shadow_daily_runner.main preflight \
  --through 2026-09-09 \
  --archives /fixed/path/weekly_2026_W*.zip \
  --trading-calendar shadow_daily_runner/runtime/trading_calendar.csv \
  --source-coverage shadow_daily_runner/runtime/audit/historical_source_coverage.json
```

只有 `historical_health_audit.json` 的所有 checks 都通過，既有 target-day readiness 與
provider audit 才會繼續；任何失敗都不會建立 zero-signal day 或修改歷史 ledger。
如果 retry 已有含今日的合法 archive，前置檢查會只評估明確指定的
`--through` 範圍；較晚日期只列為 out-of-scope，不會被誤當成歷史異常。

一次性全量修復（只寫 data/audit layer，與 `attempt` 共用同一把 process
lock）：

```bash
python3 -B -m shadow_daily_runner.main repair-history --through 2026-09-08
```

這個指令會以正式 calendar 重比每個 session 的 TWSE/TPEx 官方 EOD，不覆寫舊
archive，只建立 content-addressed patch / normalized base 並原子切換 active manifest。
執行前後四個 prospective ledger 的 byte hash 必須完全相同。

成功時只呼叫既有 `prospective_shadow_v01 run-daily`。該函式本身在 ledger 驗證前已同步
執行 `update_outcomes_from_snapshot`，所以 runner 不重複載入一次資料或建立多餘的
outcome run manifest。完成後再跑既有 `status`，確認 hash chain 及
`actual_orders = actual_fills = broker_connections = 0`，最後才寫 success marker。

## launchd

獨立 preflight plist 在每週一至週五台北時間 14:15 先以本地 immutable
inputs 檢查至前一個交易日，不下載當日 EOD、不呼叫 strategy，也不寫 ledger。
正式 runner plist 明列 14:30、15:00、15:30、16:00 四次嘗試。14:30 是首次
當日 EOD 嘗試，不代表資料已完整。Mac 睡眠造成的 16:05 後延遲觸發會由
runner 拒絕。

安裝：

```bash
./shadow_daily_runner/install_launch_agent.sh
```

驗證：

```bash
plutil -lint shadow_daily_runner/launchd/com.linyunyan.warrantscope.shadow-daily.plist
plutil -lint shadow_daily_runner/launchd/com.linyunyan.warrantscope.shadow-preflight.plist
launchctl print gui/501/com.linyunyan.warrantscope.shadow-daily
launchctl print gui/501/com.linyunyan.warrantscope.shadow-preflight
```

這是使用者登入後的 LaunchAgent；Mac 必須保持登入、開機並可連線。日誌位於
`shadow_daily_runner/runtime/logs/`，runtime 不納入 Git。若 16:00 嘗試仍未通過，當日
維持未封存，runner 不會隔日自動回填。
