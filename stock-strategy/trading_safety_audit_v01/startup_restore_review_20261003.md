# 啟動恢復檢查：2026-10-03

## 結論

**NOT_READY：完成 source/mock 修補，不代表正常 LIVE 已恢復。**
本輪沒有啟動 LIVE、送單、撤單、改單、重啟 Bot／LaunchAgent，也沒有套用
正式帳本修復、清 HALT、修改 baseline／metadata／checkpoint／Keychain。
實際帳戶、券商委託識別碼、成交明細及私人證據不得提交 Git。

## 已完成

- 保留既有未提交／未追蹤資料；先安全 fast-forward-only 同步 main，當時
  origin/main 已是最新版本。
- 兩種正式回報 normalizer 使用原生 `OrderDate`／`OrderTime`，不以收到
  callback 或查詢當下的時間冒充成交時間。明確 `TradeDate` 與原生日期
  衝突、非法日期／時間均拒絕；缺資料保持未知。
- 新增唯讀 `order_type`，直接讀原生 `OrderType`。不把 real-report
  `TradeKind` 的買賣／撤改動作解釋成庫存的融資融券種類，不推測現股。
- 新增 **離線、單次事故** 的 `incident_repair`；不接到 runtime 自動路徑，
  不匯入券商／session／通知，也不是 clear-halt 或 LIVE 啟用工具。
- 獨立 review 與 mock 回歸驗證修復不能超賣、不能侵入原庫存、不能
  錯配 basket／日期／類型，不能重播重複成交或把舊快照當成目前庫存。

## 唯讀實際查核邊界

主代理透過既有安全憑證設定與 SDK 查詢正式帳戶、回報及庫存。
獨立 wrapper 只允許登入／關閉和明確唯讀查詢，拒絕交易 API；測試該
wrapper 的 11 個防護檢查通過。查詢期間檢查 11 個受保護 runtime 檔案
未改動。native 輸出和精確證據僅留私人 temporary directory，不提交。

最後一份私人證據時間是 `2026-10-03T07:53:17Z`；這是當時的快照，
不是之後或下個交易日的即時對帳。SDK 的 close helper 會吞掉原生關閉
錯誤，所以不宣稱已取得原生登出／關閉的完整認證。

真實證據與私人 SQLite 副本的 `build_plan` 檢查結果：
`EXPLICIT_INCIDENT_MAPPING_REQUIRED`。副本 bytes 未改變；正式 `apply_plan`
呼叫 **0**。没有捏造人工核准或用虛構證據套用真實帳本。

## 離線修復工具的條件

`build_plan(snapshotDB, evidence, misbound_entry_id, current_entry_id, baseline)`
只讀穩定副本；`apply_plan(DB, plan, backup_dir)` 才能套用已審核方案。
兩者不是使用者一般啟動指令。调用者必须停掉所有控制程序、持有
runtime／帳戶排他鎖、核實來源和帳戶、保護私人證據／備份。

套用至少要求：

- 明確人工核准本次精確歷史映射，不以「只剩一筆待送」或同股票推測。
- 原始拒單與失敗 request 證據；原生日期／時間、委託與逐筆成交一致。
- 原生 OrderType／APCode，以及人工單 price type／TIF 的證據；未知拒絕。
- 策略該股原 baseline 為零、人工實際賣出不超過已證明買入量；其他
  策略部位不在此工具分配範圍。
- 僅接受已驗證可相容重播的 legacy `OrderNo:SeqNo` 成交鍵。
- 套用前、commit 前券商快照均在 300 秒內；精確 DB／方案 precondition。
- 私有備份與 SHA256、单一 SQLite 交易、完整性與外鍵檢查、失敗回滾、
  前後 audit、重複套用防護。

人工單部分成交但沒有終態證據時，保留 `PARTIALLY_FILLED` 與剩單阻擋，
不能猜已取消／失效。即使修復成功，工具始終回報
`normal_start_ready=False`，保留 HALT、原 baseline、MFE checkpoint、
intent／fingerprint／歷史事件及 next-identify。

## 尚未解除的正式啟動阻擋

1. 正式 basket 與本機 basket 不一致；文件沒有已驗證的替換／映射契約。
   必須確認當時是否有其他交易程式及該筆委託來源，不能放寬所有權比對。
2. 正式帳本尚未修復，仍有事故策略剩倉；不能吸收到原始 baseline。
3. 人工委託剩量的終態及部分委託類型證據未完整；禁止捏造終態。
4. HALT、帳戶 provenance 與交易日 baseline 仍須在安全對帳後處理。
5. 另發現未修復的收盤交易型態限制：目前 main 的 phase 將
   13:23–13:29:50 全列 MARKET，而 fallback 是 MARKET IOC；13:25後
   正式規則只接受限價 ROD。不能以現有 mock 通過認定這段正式可用。
   [元大官方逐筆交易 FAQ](https://www.yuanta.com.tw/eyuanta/Securities/QA/Index)

本輪未改 execution routing／LIVE gate，故第 5 項仍列為後續必修，不能
忽略它並宣稱 production ready。原三方安全限制不為了啟動而繞過。
已安裝服務的 source 路徑指向目前 root checkout，不需要搬動私人 runtime。
但它會啟動具正式減倉能力的 supervisor，不能把重啟說成單純 source 同步。

## 最終測試

| 範圍 | Python | Run | Pass | Skip | Fail/Error | 禁止邊界嘗試 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 完整 guarded discovery | 3.12 | 1012 | 1011 | 1 | 0 | 0 |
| Bot runtime suite | 3.10 | 253 | 253 | 0 | 0 | 0 |
| broker suite | 3.10、3.12，各 | 115 | 114 | 1 | 0 | 0 |
| temporal/order-type focused | 3.10、3.12，各 | 20 | 20 | 0 | 0 | 0 |
| incident repair focused | 3.10、3.12，各 | 25 | 25 | 0 | 0 | 0 |
| 獨立 reviewer focused | 3.10 | 45 | 45 | 0 | 0 | 0 |
| Node website | Node | 75 | 75 | 0 | 0 | 不適用 |

suite 有重疊，不能加總成獨立測試數。SDK test 跳過，不是正式交易認證。
預期 mock 故障／假通知輸出不代表真的登入或傳送通知。

重現：從 `stock-strategy` 執行
`python3 -B -m trading_safety_audit_v01.run_mock_tests --start-dir .`；
Bot interpreter 可改 start-dir 為 `yuanta_live_runtime_v01/tests`。
從 repo root 執行 `node --test tests/*.test.js`。

## 本輪提交檔案

```text
stock-strategy/yuanta_broker_execution_v01/adapter.py
stock-strategy/yuanta_broker_execution_v01/tests/test_broker_report_dates.py
stock-strategy/yuanta_live_runtime_v01/incident_repair.py
stock-strategy/yuanta_live_runtime_v01/tests/test_incident_repair.py
stock-strategy/trading_safety_audit_v01/startup_restore_review_20261003.md
stock-strategy/trading_safety_audit_v01/startup_restore_results_20261003.json
```
