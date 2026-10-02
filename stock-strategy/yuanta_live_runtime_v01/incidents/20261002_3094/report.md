# 2026-10-02 3094 聯傑未自動出場事故報告

## 結論

這不是停損或移動停利規則沒有觸發，而是 09:13:45 前交易主循環已終止。

根因是元大 `SendStockOrder` 對第二次單筆送單回傳了重複的 `Identify=1`，本機卻把 `Identify` 當成跨日永久唯一鍵。3094 的委託結果與兩筆成交因此被寫入 2026-09-24 的舊 3605 訂單；真正的 3094 訂單持續停在 `SEND_PENDING`。15 秒 ACK timeout 對帳時，本機與券商狀態不一致，觸發 `ReconciliationMismatch` 與 `STOPPED_UNSAFE`。

事故後沒有 `POSITION_OPENED`、`EXIT_SUBMITTED`、`ENTRY_CANCEL_SENT` 或 `POSITION_CLOSED` 紀錄。因此 13:20 強制出場、固定停損與 MFE 移動停利都沒有機會執行。

## 已保存證據

不可變事故備份：

`/Users/linyunyan/Downloads/WarrantScope_incident_backups/20261002_3094_20261002T151056`

內容包含正式 runtime、SQLite/WAL/SHM、session、signal ledger、critical ledger、stdout/stderr、LaunchAgent plist、process/launchctl snapshot、完整 10/2 行情封存與 SHA-256 清單。備份已移除寫入權限。

## 實際版本與程序

- 程式 checkout：`/Users/linyunyan/Downloads/WarrantScope_tracker_web_v1_2026-09-01_COMPLETE/stock-strategy`
- 程式 HEAD：`ed22cb6d373551514db3f9e363febdafac295128`
- 正式 runtime：`/Users/linyunyan/Downloads/-warrantscope-tracker/stock-strategy/yuanta_live_runtime_v01/runtime`
- LaunchAgent PID 70707 是 Telegram/control Bot service，不是已死亡的交易 child。
- heartbeat 最後記錄的交易 child PID 是 71543，最後時間為 2026-10-02 09:13:45 台北時間。

## 事件時間線（台北時間）

| 時間 | 證據事件 | 結果 |
|---|---|---|
| 08:37:14 | `ARCHIVE_STARTED` | LIVE 行情封存開始 |
| 08:37:23 | `RECONCILIATION_PASSED` / `RUNTIME_STARTED` | 啟動對帳通過 |
| 09:13:30.146 | `ENTRY_GATE_DIAGNOSTICS` | 3094 LONG 通過 |
| 09:13:30.189 | `SIGNAL_DETECTED` | 訊號成立 |
| 09:13:30.231 | `RISK_APPROVED_CANDIDATE` | 風控核准 |
| 09:13:30.236 | `ORDER_RESERVED` | 3094 本機 order `e54f...` |
| 09:13:30.532 | `BROKER_REQUEST_CREATED` | 3094 durable identify = 2 |
| 09:13:30.951 | `ENTRY_SUBMITTED` | BUY 2,000 @ 70.1 |
| 09:13:30.960 | `BROKER_REQUEST_RESULT` | 券商回傳 identify = 1、order no = J00bE，錯綁舊 3605 order `96d3...` |
| 09:13:30.999 | `FILL_RECORDED` | 1,000 @ 70.0，錯寫舊單 |
| 09:13:31.013 | `FILL_RECORDED` | 1,000 @ 70.1，錯寫舊單；均價 70.05 |
| 09:13:45.461 | `BROKER_EXECUTION_HALTED` | `RECONCILIATION_MISMATCH` |
| 09:13:45.465 | `RUNTIME_FATAL_ERROR` | `ReconciliationMismatch` / stage `RUNNING` |
| 09:13:45.468 | `RUNTIME_STOPPED_UNSAFE` | CRITICAL 寫入本機 ledger |
| 09:13:46.349 | `ARCHIVE_FINALIZED` | status `FAILED` |

## 第一個例外與可證明界線

既有程式只記錄例外型別，child stdout/stderr 又被導向 `/dev/null`，所以事故當下的完整 Python traceback 與 `ReconciliationMismatch` 訊息沒有留下。不能把推論冒充成原始 traceback。

依 15 秒 entry timeout、SQLite 狀態與程式路徑，可定位到：

- `yuanta_live_runtime_v01/main.py`：entry ACK timeout 後呼叫 `adapter.reconcile(...)`（事故版本約第 1524 行）
- `yuanta_broker_execution_v01/adapter.py`：對帳不一致後丟出 `ReconciliationMismatch`（事故版本約第 683 行）

修正版已把 exception message 與 `traceback.format_exc()` 寫入 `RUNTIME_FATAL_ERROR`，未來不再只剩 `RUNTIME_FAILED` 外殼。

## 出場、watchdog、通知與 rescue 實際行為

- 出場 trigger：沒有發生。3094 fill 未進入正確訂單，local strategy position 沒有建立。
- 出場 submission / reject / partial / unfilled：全部沒有證據；不可宣稱有送出場單。
- watchdog：LaunchAgent 保持 Bot service 存活，但沒有獨立監督已 detach 的交易 child，也沒有在 child 終止後自動做 broker-backed exit-only takeover。
- 通知：本機 CRITICAL ledger 成功；Telegram critical 使用 daemon thread，child 立即結束時沒有送達保證。stdout 同日也有大量 Telegram timeout，無法證明使用者收到事故通知。
- rescue：沒有自動 rescue。這避免在沒有 fresh broker position/open-order proof 時盲目重送，但也留下未受管理曝險。

修正版會等待 bounded critical-delivery thread；仍不會盲目自動重啟或送 rescue 單。獨立 supervisor 若日後實作，必須先取得 fresh broker positions、open orders、fills，且只允許減倉/平倉。

## 最小修正

1. `SendStockOrder` result 只可綁定「當日唯一 pending broker request」；若不唯一即 HALT，不猜測。
2. 若 broker `Identify` 重複但當日只有一筆 pending request，記錄 `BROKER_RESULT_IDENTIFIER_REMAPPED` 後安全重綁。
3. real report 先用 runtime 生成的 `BasketNo` 找訂單，再退回 broker order number。
4. broker order number 跨日重用時，只允許釋放「較早交易日且已 terminal」的舊索引；事件仍保留完整 audit。
5. fatal log 保存 exception message 與 traceback。
6. shutdown 會 bounded join CRITICAL Telegram delivery；本機 ledger 仍先 fsync。

## 測試矩陣

- 事故重現：broker 第二次回傳 `Identify=1`，修正前穩定把新單錯綁舊單；修正後 ACK 與 2 筆 fill 正確歸到新單。
- ambiguity：兩筆當日 pending request 時立即 HALT，不猜測。
- reused order number：BasketNo 優先，跨日 terminal order number 可安全轉移索引。
- duplicate fill：既有 idempotency 測試保留。
- partial fill / cancel / exit partial：既有 adapter 與 accounting state-machine 測試保留。
- post-fill restart：既有 position checkpoint/recovery 測試保留。
- stuck heartbeat / missing heartbeat：既有 watchdog 測試保留。
- stale quote / quote integrity：既有 safe quote 與 quote-integrity 測試保留。
- exit reject/unknown/reconciliation：既有 rescue reconciliation 測試保留。
- manual partial sale：新增 broker 1,000 / local 2,000 mismatch 測試，必須 HALT。
- 13:20：既有 `HARD_EXIT` exact policy test 保留。

## 殘餘 1,000 股風險

使用者陳述已在盤後手動賣出 1,000 股 @ 70.30；本機正式 runtime 沒有這筆人工成交資料。依本次限制未登入券商，因此目前只能標為 `EXPOSURE_UNKNOWN`，不能標示 clean、flat 或 safe。

不得修改正式 SQLite、偽造 manual fill、清除 HALT 或把剩餘部位吸收到 baseline。剩餘 1,000 股是否仍存在，必須由使用者在元大官方介面確認，或之後由獲准的 fresh broker position/open-order/fill query 證明。
