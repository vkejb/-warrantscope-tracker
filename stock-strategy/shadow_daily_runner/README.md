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

Raw response 以 SHA-256 content address 保存；clean ZIP 固定 member timestamp、排序與
內容，檔名同時含 source-manifest hash 與 clean hash。若同名 immutable 檔內容不同會
fail closed。

`trading_calendar.csv` 只有 `date` 欄、ISO `YYYY-MM-DD`、升冪且 unique；旁邊的
`trading_calendar.metadata.json` 保存官方 URL、retrieval timestamp、raw source hash、
derived calendar hash 與推導方法。

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
python3 -B -m shadow_daily_runner.main attempt
python3 -B -m shadow_daily_runner.main status
```

`prepare` 可在白天先建立／檢查資料，不會寫任何 prospective ledger。`attempt` 沒有
日期參數，只接受台北當日 14:25～16:05；2026-09-07 或更早一律拒絕，避免 backfill。
同日成功後的 retry 為 no-op。

Readiness 同時要求 calendar target、完整 session、0050、TWSE/TPEx 個別 coverage、
total coverage、zero duplicate、zero unresolved invalid、固定 source hash，最後還必須讓
既有 `ExistingDailyDataProvider.load_through()` 自己完整通過。失敗只寫 runner audit／
attempt report，不呼叫 `run-daily`，因此不會把資料缺口寫成 zero-signal day。

成功時只呼叫既有 `prospective_shadow_v01 run-daily`。該函式本身在 ledger 驗證前已同步
執行 `update_outcomes_from_snapshot`，所以 runner 不重複載入一次資料或建立多餘的
outcome run manifest。完成後再跑既有 `status`，確認 hash chain 及
`actual_orders = actual_fills = broker_connections = 0`，最後才寫 success marker。

## launchd

plist 明列每週一至週五台北時間 14:30、15:00、15:30、16:00 四次嘗試。14:30 是
首次嘗試，不代表資料已完整。Mac 睡眠造成的 16:05 後延遲觸發會由 runner 拒絕。

安裝：

```bash
./shadow_daily_runner/install_launch_agent.sh
```

驗證：

```bash
plutil -lint shadow_daily_runner/launchd/com.linyunyan.warrantscope.shadow-daily.plist
launchctl print gui/501/com.linyunyan.warrantscope.shadow-daily
```

這是使用者登入後的 LaunchAgent；Mac 必須保持登入、開機並可連線。日誌位於
`shadow_daily_runner/runtime/logs/`，runtime 不納入 Git。若 16:00 嘗試仍未通過，當日
維持未封存，runner 不會隔日自動回填。
