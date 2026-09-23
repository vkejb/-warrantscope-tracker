# Yuanta Live Runtime V0.1

這個模組把目前 repo 已存在的三層串起來：

`Stage A Top30 → 即時逐筆/五檔 → frozen direction rule → APPROVED intent → risk boundary → yuanta_broker_execution_v01 → SPARK SendStockOrder`

它**沒有移除** broker adapter 的安全鎖。正式送單仍必須同時滿足：

1. `EXECUTION_MODE=LIVE`
2. `ENABLE_LIVE_TRADING=YES`
3. CLI 明確收到 `--live`
4. 啟動後 `GetRealReport + GetRealReportMerge + GetStoreSummary` 對帳成功
5. persistent broker halt 未啟動

因此「解鎖」是補上可運行的正式 runtime，而不是把 fail-closed 保護刪掉。

## 策略

即時訊號沿用 `yuanta_intraday_shadow_v01.direction_follow_backtest.SPEC` 的 frozen 規則：

- 每 30 秒判斷
- 60 秒方向量、5 分鐘大單參考、3 分鐘突破
- volume delta / large-trade delta / VWAP / order-book imbalance / spread
- 09:35 起進場，13:10 後不再新開倉
- 單日最多一次進場嘗試
- 預設資金上限 190,000 元，以整張 1,000 股 sizing
- 淨損失 -5,000 元停損
- 2% 啟動移動停利、2% 回撤出場
- 虧損後回正達規則門檻出場
- 反向訊號連續確認出場
- 13:20 強制出場

實際進出場使用 broker 回報的成交股數與平均成交價，而不是 replay 的假成交。

## macOS 憑證

沿用既有 `yuanta_intraday_shadow_v01.yuanta_keychain`，PFX 路徑、PFX 密碼、證券帳號、電子交易密碼不寫入 repo。

## 第一次啟用

從 `stock-strategy` 目錄執行：

```bash
python3 -m yuanta_live_runtime_v01.main status
python3 -m yuanta_live_runtime_v01.main preflight-prod
```

若同一元大帳戶原本就有人工持股，先**確認那些持股確實不屬於本策略**，再明確建立 baseline：

```bash
python3 -m yuanta_live_runtime_v01.main baseline-prod --accept-existing-positions
python3 -m yuanta_live_runtime_v01.main preflight-prod
```

baseline 不會在 `start-prod` 時自動吞掉現有庫存；沒有明確建立 baseline 而庫存不符時會 fail closed。

## 先看即時訊號、不下單

```bash
python3 -m yuanta_live_runtime_v01.main observe-prod
```

`observe-prod` 使用 PROD 即時行情與同一套訊號判斷，但不會呼叫 `SendStockOrder`。

## PROD 正式自動交易

```bash
EXECUTION_MODE=LIVE ENABLE_LIVE_TRADING=YES \
python3 -m yuanta_live_runtime_v01.main start-prod --live
```

如果要允許 SHORT，必須明確指定經帳戶資格確認過的元大委託種類；runtime 不會猜：

```bash
EXECUTION_MODE=LIVE ENABLE_LIVE_TRADING=YES \
python3 -m yuanta_live_runtime_v01.main start-prod --live \
  --short-entry-order-type 9 --short-cover-order-type 9
```

`9` 是否適用必須以你的元大帳戶與標的實際可用交易種類為準。未指定時，SHORT 訊號只會被略過，不會誤用現股/融券類型。

## 緊急停止

另一個 Terminal 執行：

```bash
python3 -m yuanta_live_runtime_v01.main kill --reason "manual stop"
```

running process 看到 persistent `EMERGENCY_STOP` marker 後會停止新開倉、取消尚未完成的 entry，並對已成交策略部位送出平倉。平倉後 broker store 進入 halt。

如果原本的 runtime 已經中斷，marker 仍會阻止普通啟動。要做「只准退場、不准新開倉」的復原啟動：

```bash
EXECUTION_MODE=LIVE ENABLE_LIVE_TRADING=YES \
python3 -m yuanta_live_runtime_v01.main start-prod --live --recover-emergency
```

確認 broker 無未決委託、策略部位已平，再解除：

```bash
python3 -m yuanta_live_runtime_v01.main clear-halt --reason "manual verification complete"
```

## 重要限制

- 目前策略本身仍是從研究規則搬到 live runtime；「可以下單」不等於已證明有正期望值。
- SHORT 不自動猜 `StockOrderType`。
- 任一送單結果不明會沿用 adapter 的 UNKNOWN + persistent halt，不自動重送。
- 無持倉時若即時行情長時間中斷，runtime 會重建 SPARK session 並重新 reconciliation 後才繼續；有曝險時不會盲目重連送單，而會轉成退場優先。
- 行情在持倉期間長時間 stale 會 fail closed，不會用猜測價格繼續操作。
