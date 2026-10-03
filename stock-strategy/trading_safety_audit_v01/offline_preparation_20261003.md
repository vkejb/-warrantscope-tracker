# 2026-10-03 離線準備與追加防護

## 範圍

延續「先做 mock，不部署、不登入券商、不啟動 LIVE」的授權。
本輪先安全 `git pull --ff-only origin main`，結果 Already up to date；
基礎版本為 `75d65f4638db1f5ac8e20bf6b26288e65871754c`。
既存未提交研究資料和未追蹤檔案保留，不提交、不刪除。

沒有啟動服務、查 Keychain、載入正式 SDK、登入券商或送單，沒有修改
正式 HALT、SQLite、baseline、帳戶鎖、LIVE gate 或 LaunchAgent。
沒有變更選股、進場、停損、MFE 利潤保護、有效部位規模或強制退出時點。

## 能離線完成的事項

### 帳戶鎖的追加修補

複審以臨時檔案重現：既有帳戶鎖內容是合法 JSON 但缺少欄位時，原程式
可能覆寫紀錄，讓原 runtime 綁定消失。另有省略 runtime 參數時，將有效
舊綁定寫成空值的路徑。本輪已修補：

- 完整驗證版本、環境、帳戶指紋、instance、PID、時間與 canonical runtime。
- 缺欄位、錯誤身分、重複 key、過大／損壞／空白的既有紀錄一律拒絕，
  不覆寫、不刪除 lock inode，不把未知紀錄當首次使用。
- 以 `O_EXCL` 區分真正新建；省略參數仍保留已有 runtime 綁定。
- runtime 路徑在開鎖檔之前驗證，非法參數／symlink loop 不留下新鎖檔。
- health probe 同樣拒絕損壞 provenance。測試只用 synthetic 帳號與 temp roots。

### 離線唯讀檢查工具

新增 `trading_safety_audit_v01.offline_readiness`。只使用標準函式庫和明確
指定的檔案，不 import 正式 runtime、store constructor 或通知模組。
輸出只保留代碼／安全摘要，不回傳原始帳戶、股票持倉、數量、憑證、
private path 或例外字串。

檢查封存雜湊／30 檔唯一性、交易日曆前一交易日、baseline provenance、
heartbeat metadata、持久標記、plist 的 source/runtime 路徑及本機 store。
不會把 heartbeat metadata 當 process/account-lock 健康證明。

SQLite 不連原始 runtime。若有非空 WAL/journal，就標示 UNVERIFIED；
其他情況只查穩定主檔的私有暫存副本，並檢查原檔與 sidecar 前後是否
變動。暫存位置不受 ambient TMPDIR 引導到原 runtime。
本地副本沒有曝險也不代表券商空倉。

所有結果固定 `NOT_LIVE_CERTIFIED`。正常完成診斷退出碼 **3**；
無效設定退出碼 **2**。這是診斷工具，不是開 LIVE 的授權 gate，
不能接成「錯誤就清 HALT／重送委託」的自動腳本。

package 與 tests 的 side-effect-free `__init__.py` 也已補齊，避免標準
unittest 完整 discovery 跳過原先獨立的稽核資料夾。

## 真實本機檔案的唯讀結果

目標交易日採本機 calendar 的 `2026-10-05`，不是替正式券商做交易日認證：

- calendar 前一交易日為 10/2；10/2 Stage A 封存雜湊、日期及 30 檔唯一性有效。
- 既有 baseline JSON 存在，但 `position_baseline.meta.json` 缺失；不能離線
  補寫新的日期／帳戶來源，假裝已做當日 broker baseline capture。
- heartbeat 過期，最後狀態為 `STOPPED_UNSAFE`。本輪沒有重啟或接管程序。
- 穩定的本地主檔私有副本顯示 HALT、未確認委託及本機曝險紀錄；這些
  不是券商目前庫存證據。只保存阻擋摘要，不把私人訂單／數量放進 Git。
- plist 的程式來源是本輪 checkout，runtime 位於另一個實體 checkout。
  該目錄的 cached Git HEAD 為 `fc0412a`，與本輪基礎版本不同。這是
  source／state 配置差異，不等於服務已載入新版本，也不是自動覆蓋理由。
  本輪沒有同步該目錄、搬動 DB 或更改 plist。

實際診斷額外加上 Python audit hook，禁止網路、subprocess、vendor load、
原 runtime／輸入檔案的寫入／刪除／更名，以及原 SQLite connection。
禁止邊界嘗試為 0。正常 exit 3 不表示 mock 測試失敗。

## 尚不能離線解決的事項

1. 需另行授權正式**唯讀**登入，取得實際庫存、未成交委託、成交回報及
   可用資金。設定上限 NT$190,000 不是 buying power 或交割資金證明。
2. 逐筆確認舊委託／成交所有權與本機差異後，才能設計可稽核的舊帳修復。
   本輪沒有刪 DB、清 HALT、猜配回報或把策略曝險吸收到原庫存 baseline。
3. 當天 baseline 建立後人工賣出原持股，不能靠離線自動重設 baseline
   解決；必須依當日券商實際狀態處理。
4. 正式 SDK callback/DTO、行情連線、資金、通知投遞及服務實際載入版本
   仍未驗證。正式部署／服務重啟需要另外授權，不能與 mock 完成混為一談。

## 測試與重現

先重新跑既有完整 suite，再做追加修補；最終測試數與結果見
[offline_results_20261003.json](offline_results_20261003.json)。
整合測試發現過 parser 深度依賴 interpreter 的問題，已改為明確的輸入
深度限制；獨立複審要求 symlink loop 脫敏與固定私有 temp 邊界。
final 結果只採全部修補穩定後的重跑，不以先前局部通過取代整合驗證。

最終完整 suite **967 項：966 通過、1 正式 SDK test 跳過、0 失敗／錯誤**。
Bot interpreter runtime **228/228**；稽核套件 **60/60**；checker **31/31**
（3.10 和 3.12 均通過）；帳戶鎖 **17/17**；網站 Node **75/75**。
suite 有重疊，不加總成獨立測試數；完整 discovery 現在包含原先獨立的
29 項稽核回歸與新增 checker。所有 guarded Python 測試禁止邊界嘗試 0。

從 `stock-strategy` 執行完整 guarded suite：

```sh
/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir .
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir yuanta_live_runtime_v01/tests
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir trading_safety_audit_v01/tests
```

離線診斷的明確路徑／日期指令見 [README.md](README.md#offline-preparation-follow-up)。
原 10/3 修補報告不覆寫；本輪 source/mock 準備完成不等於 LIVE 已就緒。

## 本輪檔案

相對 `stock-strategy/`，只提交：

```text
trading_safety_audit_v01/README.md
trading_safety_audit_v01/__init__.py
trading_safety_audit_v01/offline_readiness.py
trading_safety_audit_v01/offline_preparation_20261003.md
trading_safety_audit_v01/offline_results_20261003.json
trading_safety_audit_v01/tests/__init__.py
trading_safety_audit_v01/tests/test_offline_readiness.py
yuanta_live_runtime_v01/account_lock.py
yuanta_live_runtime_v01/tests/test_account_lock.py
```
