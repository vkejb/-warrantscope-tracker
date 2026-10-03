# 2026-10-03 安全修補與 mock 驗證

## 結論與範圍

本輪完成程式碼安全修補與 mock 故障回歸；**不代表正式帳戶或部署已就緒**。
修補前版本為 `689d07aa95c160b9b798d81a0e2a570b83a8eb95`，開始前已安全
fast-forward-only 同步。原始 [稽核報告](report_20261003.md) 保留，不覆寫歷史證據。

依本輪授權，沒有部署／重啟 Bot 或 LaunchAgent、登入券商、啟動 LIVE、
送真實委託、讀取正式 Keychain 憑證，或修改真實 runtime／HALT／原庫存
baseline。沒有調整選股、進場條件、有效整張部位規模、初始停損、MFE
利潤保護門檻、13:20／13:23 時點或 LIVE 授權 gate。

## 修補摘要

| 防線 | 完成的 source / mock 修補 |
|---|---|
| 回報所有權與去重（B01–B10） | request、account、symbol、side、basket／交易日交叉核對；完成 ACK 與成交去重持久化；亂序回報不倒退終態；跨日重用成交序號不碰撞；改價／減量只採權威有效數量及最新 mutation 證據。未知身分仍 HALT，不猜配。 |
| 最後送單防線（E08、R01/R02/R04） | 在 durable write 和 payload 建立之後、API 呼叫之前再次檢查；減倉依 broker 與 local 的同一融資類別差額、不碰 baseline、不超賣、不重複退出；非法整張大小、非有限值或未知風控輸入拒絕。 |
| 對帳與逾時 | 查詢採單一 bounded deadline；格式錯誤、缺失或截斷的回報不能當作空庫存；失敗／逾時 session 不再用晚到回報冒充新快照；只有可持久證實「從未送出」的拒絕單可排除 missing-remote，UNKNOWN 委託仍阻擋。 |
| 主退出（E01–E07、E09–E11） | 取消失敗／未確認會再對帳及重試；部分、late fill、已成交退出後殘量持續管理；13:20 主退出、13:23 fallback 與 cutoff 告警不被 pending 分支跳過；各持倉報價獨立檢查，支援新鮮五檔的單邊退出價，不放寬進場行情。 |
| 例外恢復 | 保持禁止新倉的退出恢復；heartbeat／log／archive／通知失敗不擋已有曝險處理。checkpoint 損壞只在明確 exit-only 且 broker 實際成交與部位一致時重建退出所需數量／成本；不猜 MFE，不恢復一般進場。 |
| 雙程序、監督與狀態（S01–S06、S08/S09） | 跨 checkout 的帳戶＋環境 kernel lock、instance 身分與持久 runtime 綁定；活著但無法證明健康的 owner 不被接管。scheduler 異常不靜默存活；授權條件下退出 child 依 backoff 持續恢復到 cutoff。新鮮心跳不等於新鮮行情。 |
| 通知（S07） | durable outbox、lease、退避重送、重新載入憑證；區分 API 確認與 UNKNOWN。採 at-least-once，可能重複；不把 queued 當成使用者已收到。 |
| 成本／風控輸入 | 跨日緊急減倉不再因純跨日記帳中斷退出；保守稅費估值與當日歸屬，非法／NaN 稅費拒絕。同日既有費用模型不變。估計不等於券商正式交割帳。 |

獨立複審另找到並修補：已證實未送出仍永久卡對帳、最後 guard 太早、
持續寫檔失敗阻礙退出、格式錯誤的 snapshot 被當空庫存、損壞 checkpoint
阻擋 broker-proven exit-only，以及狀態誤報市場監控。複審與回歸沒有再
找到可重現的 P0／P1；這不是對正式 SDK 的完整安全認證。

## 實際測試結果

先跑既有測試，再改 source／新增回歸，最後重跑完整 suite。
各 suite 重疊，不能把下表加總成獨立測試數。

| 階段／範圍 | Interpreter | 執行 | 通過 | 跳過 | 失敗／錯誤 |
|---|---|---:|---:|---:|---:|
| 修補前 runtime | Bot Python 3.10.0b4 | 145 | 145 | 0 | 0 |
| 修補前 broker | Bot Python 3.10.0b4 | 40 | 39 | 1 | 0 |
| 修補前 stock-strategy 全套 | Python 3.12.14 | 769 | 768 | 1 | 0 |
| 修補後 stock-strategy 全套 | Python 3.12.14 | 899 | 898 | 1 | 0 |
| 修補後 runtime | Bot Python 3.10.0b4 | 220 | 220 | 0 | 0 |
| 修補後 broker | Bot Python 3.10.0b4 | 95 | 94 | 1 | 0 |
| 修補後 safety audit | Bot Python 3.10.0b4 | 29 | 29 | 0 | 0 |
| 網站 Node suite | Node | 75 | 75 | 0 | 0 |

所有修補後 Python suite 均透過 offline runner，禁止的 Python network、
Keychain／服務／通知 process、LIVE child、指定 vendor library 邊界嘗試
為 **0**。runner 僅修改自己的 test-process 環境，沒有改持久開關。
它是第二道檢查，不是任意原生 SDK 的 sandbox；測試本身也使用 fake API、
mock session／credential／network 與臨時 DB。stdout 的登入、Keychain、
HALT 或 CRITICAL 字樣屬測試情境，不是正式操作。正式 SDK contract
測試依這輪 mock-only 範圍跳過，沒有拿舊反射結果充作本輪正式驗證。

`git diff --check` 通過。八個策略方法 `choose_entry`、`record_tick`、
`record_book_combined`、`projected_net`、`_refresh_mfe_basis`、`_observe_mfe`、
`evaluate_exit`、`opposite_signal` 與三組策略 policy AST 均保持不變；
frozen signal spec、gate、bridge、plist 沒有任務變更。

重現命令（從 `stock-strategy` 執行）：

```sh
/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir .
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir yuanta_live_runtime_v01/tests
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir yuanta_broker_execution_v01/tests
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir trading_safety_audit_v01/tests
```

從 repo root 執行 `node --test tests/*.test.js`。機器可讀結果在
[mock_results_20261003.json](mock_results_20261003.json)。

## 仍未驗證／不在本輪權限內

1. **R03 尚未解決：** NT$190,000 是設定上限，不是真實可用資金、當沖
   buying power 或交割款。沒有杜撰帳務欄位或聲稱額度可用。
2. 正式 broker 庫存、外部委託、舊錯配成交及 baseline 缺口仍需另外授權的
   唯讀對帳與可稽核修復。本輪沒有刪 DB、清 HALT 或重抓 baseline 繞過。
3. 正式 DTO 容器／欄位、改價與減量語意、跨回報時序須在另外授權範圍
   驗證。缺少 query correlation ID 時採失敗 session 隔離；不宣稱任意
   晚到原生 callback 或 native login／close 阻塞已經端到端驗證。
4. 斷電、闔蓋睡眠、網路中斷、停牌及無流動性可能讓退出無法成交。
   防 idle sleep 不等於防斷電；沒有做真機睡眠／電源測試。
5. outbox 的 API acknowledgement 不是閱讀證明；shutdown 期間未送出的
   通知可能等下一個 worker。部分主迴圈測試是精確 AST 分支，不是完整
   正式主迴圈測試。
6. 跨 checkout 的帳戶 runtime 綁定遇舊／損壞 provenance 會要求人工檢查，不自動換目錄。
   本輪未證明已安裝服務載入此 commit，亦未執行 alias checkout／部署同步。

因此：**source／mock 修補完成；仍不能宣稱下個交易日 LIVE 已安全可用。**

## 本輪檔案清單

以下均相對 `stock-strategy/`；只提交這 32 個檔案。
既存 `prospective_shadow_v01/data/` 三個 dirty 檔案、`live_trading_assistant_v01/`、
`.DS_Store`、`__pycache__` 等無關未追蹤內容保留且不提交。

```text
trading_safety_audit_v01/README.md
trading_safety_audit_v01/repair_report_20261003.md
trading_safety_audit_v01/mock_results_20261003.json
trading_safety_audit_v01/run_mock_tests.py
trading_safety_audit_v01/tests/test_broker_failure_evidence.py
trading_safety_audit_v01/tests/test_entry_risk_evidence.py
trading_safety_audit_v01/tests/test_exit_failure_evidence.py
trading_safety_audit_v01/tests/test_startup_state_evidence.py
trading_safety_audit_v01/tests/test_supervisor_failure_evidence.py
yuanta_broker_execution_v01/adapter.py
yuanta_broker_execution_v01/store.py
yuanta_broker_execution_v01/tests/test_adapter.py
yuanta_broker_execution_v01/tests/test_safety_regressions.py
yuanta_live_runtime_v01/account_lock.py
yuanta_live_runtime_v01/accounting.py
yuanta_live_runtime_v01/force_flat_supervisor.py
yuanta_live_runtime_v01/main.py
yuanta_live_runtime_v01/notifications.py
yuanta_live_runtime_v01/risk_manager.py
yuanta_live_runtime_v01/strategy.py
yuanta_live_runtime_v01/trading_bot_notifier.py
yuanta_live_runtime_v01/trading_bot_service.py
yuanta_live_runtime_v01/watchdog.py
yuanta_live_runtime_v01/tests/test_account_lock.py
yuanta_live_runtime_v01/tests/test_carryover_accounting_safety.py
yuanta_live_runtime_v01/tests/test_force_flat_supervisor.py
yuanta_live_runtime_v01/tests/test_risk_config_safety.py
yuanta_live_runtime_v01/tests/test_risk_runtime.py
yuanta_live_runtime_v01/tests/test_runtime_safety_regressions.py
yuanta_live_runtime_v01/tests/test_status_safety.py
yuanta_live_runtime_v01/tests/test_supervision_safety_regressions.py
yuanta_live_runtime_v01/tests/test_trading_bot_service.py
```
