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

成本敏感的盤中資料回放使用 `exploratory_backtest.py`。它固定以 09:30–09:35
形成訊號、09:35 後進場及 13:25 平倉，成交價使用當時買一／賣一再加一檔
不利滑價，並計入電子交易手續費代理值、最低手續費及現股當沖交易稅。回放
只接受已驗證 SHA-256 的行情檔，結果一律標為樣本內探索，不是預期報酬；
空方結果也不代表個股已通過當沖、融券或處置資格檢查。

`direction_follow_backtest.py` 另外提供逐筆方向跟隨回放。它每 30 秒使用最近
60 秒的主動買賣量差、截至當時的動態大單門檻、VWAP、五檔失衡與三分鐘
突破判斷方向；單一 30 秒視窗成立即可進場，30 檔只取最高分的一檔，並以
19 萬元內可容納的最大整張數配置。預估平倉後淨損益達 -5,000 元即停損；
不設固定停利，未實現淨報酬至少曾達 2% 後，若由峰值回落 2 個百分點即
移動停利，另保留方向反轉與 13:20 強制平倉；若強制平倉前 60 秒內沒有
可用報價，該筆結果視為無法評分，不使用更早的價格灌入績效。結果同樣只
屬於樣本內探索。

## 交易日全自動模式

執行一次本機安裝器：

```bash
open scripts/install_yuanta_intraday_automation.command
```

安裝器以隱藏輸入將 PFX 路徑、PFX 密碼、證券帳號與電子交易密碼存入
macOS Keychain，安裝使用者層級 LaunchAgent，並設定週一至週五 08:45
喚醒。08:50 runner 仍會先查正式交易日曆與前一交易日 Stage A seal；假日
不登入。收集期間由 `caffeinate` 防止休眠，13:35 後才分析及通知。

Keychain、LaunchAgent 與喚醒狀態可用下列命令檢查，輸出不含秘密：

```bash
open scripts/check_yuanta_intraday_automation.command
```
