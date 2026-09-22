# Yuanta Intraday Shadow V0.1

這個模組目前只提供元大 SPARK API 的逐筆成交與五檔訂閱 smoke test。

安全契約：

- 不匯入任何委託物件。
- 不呼叫任何 `Send*Order` API。
- 不保存帳號、憑證密碼或電子交易密碼。
- 不建立正式或模擬委託。
- 測試結束一定嘗試解訂閱、登出及關閉連線。

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

目前尚未實作資料落盤與紙上撮合；必須先通過此 smoke test，才進入下一階段。
