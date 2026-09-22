# Yuanta Intraday Shadow V0.1

這個模組提供元大 SPARK API 的逐筆成交與五檔訂閱 smoke test，以及
Stage A Top30 append-only 即時行情收集器。

安全契約：

- 不匯入任何委託物件。
- 不呼叫任何 `Send*Order` API。
- 不保存帳號、憑證密碼或電子交易密碼。
- 不建立正式或模擬委託。
- 測試結束一定嘗試解訂閱、登出及關閉連線。
- Top30 市場別只從同日 immutable 官方 TWSE／TPEx EOD 判定，不以代碼猜測。
- 每次收集建立獨立 run 目錄，保存逐筆、五檔、輸入 seal 與 SHA-256 摘要。
- 收集完成後使用固定 V0.1 定義計算價格路徑、VWAP、量能節奏、spread 與五檔失衡。

元大 `YuantaSparkAPI.dll` 2.0.0.1 的逐筆與五檔訂閱函式實際回傳
`System.Void`；工具以「呼叫未拋出例外」表示 `REQUESTED`，並以後續收到的
事件數判定串流是否真的可用，不把 Python `None` 誤判成拒絕。

執行：

```bash
cd stock-strategy
/Users/linyunyan/Downloads/YuantaSparkAPI_osx-arm64_Python/.venv/bin/python \
  -m yuanta_intraday_shadow_v01.main --seconds 60
```

正式環境只用於讀取即時行情。輸入的是證券交易帳號，不是 CMA 交割帳號。

執行 Top30 五分鐘收集：

```bash
open scripts/run_yuanta_stage_a_top30_collector.command
```

開啟本機圖形介面：

```bash
open scripts/open_yuanta_intraday_gui.command
```

圖形介面提供五分鐘測試、早盤至 10:30、全天至 13:35 與自訂分鐘。
全天模式會以 gzip 直接寫入不可覆寫的逐筆／五檔資料，並在完成後產生
09:05、09:15、09:30、10:00、10:30 固定時間快照與收盤後路徑結果。

即時資料只寫入 gitignored 的 `runtime/runs/<run_id>/`。目前沒有紙上撮合，
也沒有任何元大委託函式；帳號與兩組密碼仍由當次互動輸入且不落盤。

衍生分析寫入獨立的 `runtime/analyses/<run_id>/`，不修改原始行情。V0.1
狀態只屬於 prospective shadow diagnostics，不是進出場建議，也尚未經歷史驗證。
