# 事故修復後續：2026-10-03

## 結論

**正式本機事故帳本已修復，正常開新倉 LIVE 仍是 NOT_READY。**

使用者確認事故當時只有 WarrantScope Bot 運作，並授權修復其餘問題。
主代理先完成 mock、私人副本與獨立 review，再以正式唯讀證據修正指定
本機 SQLite。這是一次性歷史歸帳修復，不是放寬未來委託的所有權判斷。
沒有送單、撤單、改單、啟動 LIVE、重啟 Bot／LaunchAgent或清除 HALT。
未公開帳號、憑證、原生識別碼、持股明細、精確交易價格或私人修復方案。

本報告是前一份 `startup_restore_review_20261003.md` 的後續；前一份保留
當時的未修復結論，不回填成已完成。

## 已完成的 source 修補

- 新增純函式 `GetOrderTradeReport` normalizer，核實帳戶，保留原生
  TradeDate、AcceptDate／AcceptTime、OrderType、APCode、PriceFlag、ROD／IOC／FOK、
  原委託量／成交量／取消量及成交市場別。它不自動認領成交，也不送單。
- 修復工具以精確帳戶、交易日、委託、股票、買賣、數量、價格及原生
  成交時間綁定歷史證據；AcceptTime 不假裝與 merge OrderTime 相同。
- 盤後人工單 APCode 差異，僅允許本事故中經官方原生 IO 欄位契約與唯一
  成交證實的例外。保留原始欄位，不放寬正常 callback 配對。
- 委託 ACK 不當作成交；只有 `RptType=51` 且 `OrderStatus=8` 才可附成交
  證據。時間表示 `.000`／`.000000` 僅在同一精確瞬間視為一致；沒有容差。
- 缺失 baseline metadata 的工具只接受已審核的**原始捕捉**紀錄與 hash，
  不能用今天時間、檔案 mtime 或事後庫存重建。此次沒有套用 metadata。

## 強平的交易時段修補

| 台北時間 | 已有策略部位的出口 |
| --- | --- |
| 13:20–13:23前 | 保留原限價強平流程、行情檢查及券商剩量核對 |
| 13:23–13:25前 | 正規整張的 MARKET IOC 備援 |
| 13:25–13:29:50前 | 正規整張限價 ROD；SELL 使用原生 L，既有 cover 使用 H |
| 13:29:50起 | 不再新增委託；持續不安全狀態／CRITICAL，不冒充已平倉 |

13:25後不能繼續使用 MARKET IOC，依
[元大官方逐筆交易說明](https://www.yuanta.com.tw/eyuanta/Securities/QA/Index)。
L／H 與 `Price=0`、ROD 的 wire 值使用
[元大 SendStockOrder 文件](https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/%E4%BA%A4%E6%98%93/%E5%9C%8B%E5%85%A7%E8%AD%89%E5%88%B8%E4%B8%8B%E5%96%AE/index.html)，
不猜當日數字價格。

舊出口委託若不符合新時段，先撤單，等終態與嚴格對帳才送剩餘量；
CANCEL_PENDING 或未完成 mutation 不能放行第二筆 NEW。符合收盤規格的
ROD 不會每圈被重新撤掉。零股／混合股數無已驗證備援路徑時明確告警並
fail-closed，不送不合法 MARKET IOC。不新增放空能力、不改進出場訊號、
停損／移動停利、部位大小或 LIVE gate。

限價／市價送出都不是成交保證；本輪未送真單，不能宣稱強平經實盤認證。

## 正式本機帳本修復與驗證

最新完成的唯讀快照：`2026-10-03T08:57:46Z`，不是下一交易日的對帳。
查詢器只允許登入／關閉和明確唯讀 API，拒絕所有交易 API；其 11 個
mock 防護檢查通過。最後一次查詢另有外層 55 秒程序總期限，逾時／失敗
的證據不得使用；內層 `wait(20)` 本身不涵蓋 SDK 同步呼叫的耗時。
每次完成的查詢均驗證 11 個受保護 runtime 檔案未變。

正式套用前，持有 runtime 與同帳戶 OS 排他鎖，核對帳戶與完整檔案
precondition；較早的兩來源快照在 300 秒內，commit 前再次檢查 TTL。

1. 私人副本原子套用成功；重複套用回報 `ALREADY_APPLIED`，不重複成交。
2. 正式帳本原子套用一次：歷史拒單恢復未成交拒單；事故買單歸到正確
   entry；人工實際賣出成交獨立入帳。人工原委託剩量沒有終態證據，保留
   PARTIALLY_FILLED，不虛構取消／失效。
3. 修後 local 策略剩倉，加原 baseline，與該次券商庫存逐 bucket 一致。
4. HALT、原 baseline bytes、checkpoint、原 intent／fingerprint／歷史事件、
   next-identify、其他受保護 runtime 檔案保留。沒有把剩倉吸入原庫存。
5. `runtime/incident_backups/` 保存 owner-only 備份：目錄0700、檔案0600；
   SHA256、SQLite integrity 與修前 logical digest 均已核對。
6. 獨立唯讀 postcheck 再確認修後 digest、歸帳、HALT與備份復原來源，
   驗證程序不修改正式檔案。沒有實際執行回復或再次套用正式帳本。

私人 native log、精確證據、backup／plan均不提交。SDK close helper 返回，
但它會吞掉原生關閉錯誤，因此不宣稱原生登出／關閉已完整認證。
既有帳戶鎖 provenance 在維護鎖取得時建立／更新，不是 LIVE 授權。

## 仍未解除的阻擋／限制

- 策略事故剩倉仍在；正常開新倉不能吸收它成 baseline。
- 人工部分成交委託的剩量缺原生終態證明，保留阻擋。
- HALT仍有效；不為了讓啟動成功而直接清除。
- 原 baseline 捕捉的帳戶／日期 provenance 證據仍不足，metadata不造假。
- 缺失的 MFE checkpoint 不重建；不宣稱跨日原策略監控可完整恢復。
- 本次 non-WS basket 歸屬僅經使用者確認和精確事故證據修正。
  未來 wire basket 回傳契約仍須獨立 UAT／唯讀驗證，不放寬 matcher。

現有服務 source 路徑已指 root checkout，私人 runtime仍留原位置；不搬移
runtime、不覆蓋另一份 checkout。服務未重啟。平倉之後仍須重新取得
新鮮券商持倉／委託證據與合法當日 baseline，不能承諾會自動恢復 LIVE。

## 最終測試

| 範圍 | Python | Run | Pass | Skip | Fail/Error | 禁止邊界嘗試 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 改動前完整 guarded suite | 3.12 | 1012 | 1011 | 1 | 0 | 0 |
| 改動後完整 guarded suite | 3.12 | 1076 | 1075 | 1 | 0 | 0 |
| Bot runtime suite | 3.10 | 296 | 296 | 0 | 0 | 0 |
| broker suite | 3.10 | 136 | 135 | 1 | 0 | 0 |
| incident repair，每個版本 | 3.10／3.12 | 40 | 40 | 0 | 0 | 0 |
| closing focused，每個版本 | 3.10／3.12 | 28 | 28 | 0 | 0 | 0 |
| history normalizer，每個版本 | 3.10／3.12 | 21 | 21 | 0 | 0 | 0 |
| Node | Node | 75 | 75 | 0 | 0 | 不適用 |

中間一次完整 suite 發現 AST audit fixture 少新 phase local；僅補 fixture，
不弱化 assertion，最後完整 suite 已重跑通過。副本第一次驗證也抓到
ACK／FILL區別問題，修補並增加 regression 後才套用正式帳本。
各 suite 重疊，不加總為獨立測試數；跳過SDK測試不是券商交易認證。
預期故障／通知／假登入輸出均是 mock，不代表真通知或真交易。

重現：`stock-strategy` 下執行
`python3 -B -m trading_safety_audit_v01.run_mock_tests --start-dir .`；
Bot Python可用 `--start-dir yuanta_live_runtime_v01/tests`。
repo root執行 `node --test tests/*.test.js`。`git diff --check`通過。

## 本輪提交檔案

```text
stock-strategy/yuanta_broker_execution_v01/adapter.py
stock-strategy/yuanta_broker_execution_v01/tests/test_readonly_order_history.py
stock-strategy/yuanta_live_runtime_v01/main.py
stock-strategy/yuanta_live_runtime_v01/incident_repair.py
stock-strategy/yuanta_live_runtime_v01/tests/test_incident_repair.py
stock-strategy/yuanta_live_runtime_v01/tests/test_closing_auction_safety.py
stock-strategy/yuanta_live_runtime_v01/tests/test_force_flat_supervisor.py
stock-strategy/yuanta_live_runtime_v01/tests/test_risk_runtime.py
stock-strategy/trading_safety_audit_v01/tests/test_exit_failure_evidence.py
stock-strategy/trading_safety_audit_v01/incident_recovery_followup_20261003.md
stock-strategy/trading_safety_audit_v01/incident_recovery_followup_results_20261003.json
```

既有 research/prospective未提交變動、未追蹤檔案均保留，不納入本輪 commit。
