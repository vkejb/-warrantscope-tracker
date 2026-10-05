# Yuanta Live Runtime V0.1

這個模組把目前 repo 已存在的三層串起來：

`每日擴大當沖池 → 即時逐筆/五檔 → direction rule → 0050 market gate → ANTI_CHASE_BALANCED_V1 → APPROVED intent → risk boundary → yuanta_broker_execution_v01 → SPARK SendStockOrder`

Stage A 每日 30 檔仍保留為波段／短線觀察清單與獨立通知，不再作為 LIVE
當沖候選池的上限。LIVE 使用同一資料日的 sealed expanded universe；若當日擴大池
封存不存在、日期或雜湊不符，啟動會 fail closed。元大逐筆與五檔均依每批最多
200 檔分批訂閱；每個候選批次都必須實際收到至少一檔有效逐筆與五檔，否則
preflight／runtime 會拒絕宣告行情就緒。0050 只作市場基準，不會成為交易候選。

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
- 09:05 起進場，13:10 後不再新開倉
- 做多進場另套用 `LONG_0050_RELATIVE_STRENGTH_V1`：同步訂閱 0050，寫入獨立 `market_context_*` 歸檔，不混入 expanded universe 原始檔，也不讓 0050 成為交易候選
- 0050 同時在 VWAP 上且 5 分鐘報酬非負時視為 BULLISH，個股 5 分鐘相對強度不得為負
- 0050 同時在 VWAP 下且 5 分鐘報酬為負時視為 BEARISH，個股必須至少領先 0050 0.5%，且原訊號連續確認 2 次
- 其餘為 NEUTRAL，個股必須至少領先 0050 0.25%，且原訊號連續確認 2 次
- 0050 缺少完整 5 分鐘暖機資料、行情 stale 或計算無效時 fail closed，不建立新倉
- 正式做多進場套用 `ANTI_CHASE_BALANCED_V1`：訊號價相對當日第一筆有效價不得超過 2.0%，相對當日即時 VWAP 不得超過 1.25%；任一超標即拒絕該次進場並保留拒絕原因
- 防追高只改變進場核准，不改選股、原始方向訊號、部位 sizing、停損、MFE、強制出場、broker gate 或送單路由
- 每個 30 秒決策的市場狀態、相對強度、通過／拒絕原因寫入獨立 signal ledger，供後續累積樣本驗證
- 單日最多一次進場嘗試
- 預設資金上限 190,000 元，以整張 1,000 股 sizing
- 不固定限制每檔張數；依 190,000 元單筆上限計算可買整張數量
- 同時最多 1 個策略部位、每日最多 1 次進場
- 淨損失 -3,500 元災難停損（依實際可執行報價與費稅後損益判斷，跳空可能超過）
- MFE_V1 分段鎖利：1R 保本價、1.5R 保留60% MFE、2R 保留70%、3R以上保留75%
- 鎖利價只能往有利方向移動；部分成交後重新換算部位成本，但不得放寬既有絕對鎖利價
- 不再使用「先虧損、回正即出場」；未達 1R 前仍由災難停損、反向訊號與強制出場管理
- 反向訊號連續確認出場
- 13:20 強制出場
- 13:23 若 13:20 退場仍未完成，先撤銷原退場單並重新以元大實際庫存核對
  `actual cash position - 當日 baseline`；只有 broker/local 差額完全一致時，才對剩餘差額送
  市價 IOC。原始庫存不在賣出數量內，實際庫存低於 baseline 時不反向買回。

實際進出場使用 broker 回報的成交股數與平均成交價，而不是 replay 的假成交。

`LONG_0050_RELATIVE_STRENGTH_V1` 是小樣本下的保守前瞻版本，不是已被歷史資料證明的最佳參數。現有三日資料沒有同步、完整的 0050 逐筆序列，因此沒有用缺失資料補值或倒推回測；它的用途是先避免弱市中追進相對弱股，並留下完整拒絕紀錄，等樣本增加後再做相同交易宇宙的受控比較。這次沒有改做空規則，也不會因加入 0050 自動啟用 SHORT。

### 退場規則的資料界線

舊版（含 `LOSS_RECOVERY_TO_PROFIT`）在既有 18 筆獨立訊號 replay 為淨損益
-1,574 元、PF 0.921、最大回撤 9,664 元。移除回正出場後，單獨使用 3,500 元
停損而不使用 MFE 的 16 筆可評分交易降為 -14,851 元、PF 0.495；另 2 筆因
後段行情缺失無法判定合法出場。加入 MFE_V1 後有 17 筆可評分、7 筆 MFE 出場，
淨損益 -1,778 元、PF 0.932，仍有 1 筆後段行情缺失。

為避免不同樣本誤導，只比較三種規則都可評分的相同 16 筆：舊版為 -7,894 元、
PF 0.602；移除回正出場且保留 MFE_V1 為 -6,220 元、PF 0.761，改善 1,674 元，
但仍為負值。MFE 讓部分大贏家續跑，也讓部分原本小賺交易回落；其中 1R 的
0R 價格保護是入場價格，不是扣除費稅後損益兩平，因此仍可能小幅虧損。
資料只有三個品質受限交易日，且是可重疊的獨立訊號，不可視為預期報酬、
可執行投資組合或實盤通過標準。

MFE 保護在 1R 前不會啟動；達到 1R 後也可能因回落先出場、錯過後續上漲。
跳空、滑價、委託未成交仍可能讓實際獲利低於鎖利價，因此不保證「最大程度」
保留盤中最高獲利。

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

`start-prod` 本身不會改寫 baseline；baseline 只由啟動前的獨立唯讀步驟建立，缺少或庫存不符時會 fail closed。

Trading Bot 的 `/start` 前置檢查現在會先以正式帳戶唯讀查詢自動建立或沿用「當日
baseline」。同一交易日只會建立一次；當日本機已有任何 ENTRY 委託歷史、策略部位、
進行中委託，或券商仍有未成交單時，禁止重新建立，避免盤中重啟把新增部位誤認成原始
庫存。baseline 另有日期及帳戶雜湊 metadata；不保存完整帳號或密碼。

## 獨立 13:20 force-flat supervisor

Trading Bot service 可在明確設定 `ENABLE_SCHEDULED_FORCE_FLAT=YES` 時，同時啟動獨立
排程執行緒。13:20 它建立持久 `FORCE_FLAT_REQUEST`；主交易 child 正常時由原本的
13:20 退場流程接手，child 已中斷時則以 `start-prod --live --recover-force-flat` 啟動
exit-only recovery。恢復子程序仍必須通過原本三重 LIVE gate、singleton lock、broker
reconciliation 與 baseline 差額檢查，不能建立新倉。

`FORCE_FLAT_REQUEST` 只有在重新查詢券商並確認實際庫存等於 baseline、券商與本機都無
未決委託後才刪除。到 13:23 尚未完成時，runtime 取消仍在市場的限價退場單，待取消與
部分成交完成對帳後，才以市價 IOC 送剩餘差額。委託狀態不明、外部未成交單、差額不一致、
或混合整股／零股數量不能安全編碼時，一律停止猜測並發 CRITICAL；「市價」仍不保證成交。

已安裝 Trading Bot 時，可在 Telegram 私人對話使用：

```text
/sync-baseline
/confirm 1234
```

`/sync-baseline` 必須經一次性確認碼，而且只允許在 runtime 已停止、沒有
`STOP_REQUEST`／`EMERGENCY_STOP`、券商沒有未成交委託、本機沒有策略部位或進行中
委託時執行。子程序固定使用 `DRY_RUN`／`ENABLE_LIVE_TRADING=NO`，只讀取元大正式帳戶
庫存並原子更新 baseline；不會啟動 LIVE 或送單。同步失敗時保留原基準。

## 同帳戶人工交易共存

LIVE runtime 會把每日啟動時的券商庫存視為 frozen baseline，並把程式本身的 fills
獨立保存為策略部位。每日 baseline 建立後，如果元大完整委託彙總證明另一筆人工委託
發生在 baseline 之後、帳號正確、券商委託號與時間完整、沒有未知 BasketNo，而且該股票
沒有程式持倉，reconciliation 會把其累計成交量寫入持久的
`external_inventory_adjustments` 台帳。有效 baseline 為：

```text
daily frozen baseline + verified external/manual fills
```

因此人工買賣其他股票不會被誤認為策略部位，也不會納入 13:20／13:23 強制平倉。
部分成交會依券商累計成交量單調更新，重複 callback、重連與重啟不會重複計算；人工成交
被採納時會留下 audit event 並推送通知。

安全邊界仍然 fail closed：

- 人工委託與程式既有持倉為同一股票時，停止並要求人工核對。
- 人工未成交委託與新的策略候選為同一股票時，只跳過該候選，不送單；其他股票仍可在
  下一次完整對帳後交易。
- 未知非空 BasketNo、缺少券商委託號／日期時間、不支援的交易類別、成交量倒退、或券商
  庫存無法由 baseline、外部成交與策略 fills 精確解釋時，一律 HALT。
- 轉帳、股票撥轉或其他沒有當日委託成交證據的庫存變化不會自動吸收。
- `/sync-baseline` 建立新 baseline 時會把當日外部調整折入新基準並清空該日調整台帳，避免
  重複計算。

Telegram `/start` 會先檢查 persistent broker execution HALT；若仍為 HALT，
不會產生確認碼，必須先用 `/status` 查明原因，確認元大實際庫存與未成交委託後，
再用 `/clear-halt` 解除。輸入確認碼後，Trading Bot 會等待 runtime 完成登入、對帳與
行情訂閱；只有回覆「已確認啟動，正在監控市場」，且 `/status` 顯示
「目前監控市場：是」，才代表 LIVE runtime 已真正開始監控。僅顯示「仍在啟動」
不等於已完成啟動，也不代表已送出任何委託。

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

running process 看到 persistent `EMERGENCY_STOP` marker 後會停止新開倉、取消尚未完成的 entry，並對已成交策略部位送出平倉。平倉後 broker store 進入 halt。這個不帶 `--live` 的形式只建立持久停止標記，不會自行建立第二條券商連線。

若原 runtime 已停止，需要由獨立控制程序實際登入券商、對帳並執行 exit-only 救援，仍須三重 LIVE 授權：

```bash
EXECUTION_MODE=LIVE ENABLE_LIVE_TRADING=YES \
python3 -m yuanta_live_runtime_v01.main kill --live --environment PROD \
  --reason "independent emergency recovery"
```

若 heartbeat 顯示原 runtime 仍存活，控制指令只留下停止標記，避免兩個程序同時管理同一帳戶；若原 runtime 已停止，才由控制程序進入獨立救援。

如果原本的 runtime 已經中斷，marker 仍會阻止普通啟動。要做「只准退場、不准新開倉」的復原啟動：

```bash
EXECUTION_MODE=LIVE ENABLE_LIVE_TRADING=YES \
python3 -m yuanta_live_runtime_v01.main start-prod --live --recover-emergency
```

確認 broker 無未決委託、策略部位已平，再解除。`clear-halt` 會重新登入並以元大實際委託／庫存核對 baseline，不再只相信本機 SQLite：

```bash
python3 -m yuanta_live_runtime_v01.main clear-halt --reason "manual verification complete"
```

## 重要限制

### 成交與復原可靠性

- 損益從 SQLite 逐筆成交帳本計算，包含部分成交後撤單、分批退場；未實現損益只計剩餘股數。重複成交回報不重複入帳。
- 手續費按每張委託的實際成交金額估算後分攤，不是每次 callback 各收最低費；此數字不是券商結算金額。交易日暫依原委託的台北日期，跨日沖銷會拒絕計算，需人工查核稅率與帳務。
- 退場單成交不等於已平倉：必須重新查詢券商部位及未決委託，確認回到既有庫存 baseline 且本策略無剩餘曝險。對帳失敗會停止並告警，不宣告平倉成功。
- 持倉獲利高點、虧損低點、反向訊號計數與待出場原因持久保存。舊部位若沒有 checkpoint，復原後進入待退場狀態；資料損壞則停止，不默默重設高低點。
- MFE 價格、初始 R、鎖利 R／價格與啟動時間都寫入 checkpoint。升級前的舊 checkpoint 沒有 MFE 狀態時會要求安全退場，不會把移動停利重設為較寬。
- 逐筆行情使用交易所時間並檢查延遲、未來時間、倒序與重複序號；不合格行情仍保留原始歸檔，但不更新策略價格或 VWAP。序號防護是程序內狀態，程序重啟仍需重新暖機。五檔資料仍受 SDK 提供的時間資訊限制。
- 交易所與本機 callback 時鐘允許最多 0.5 秒的正向偏差；超過此範圍的未來行情仍 fail closed。離線 LIVE-parity 重播同時使用封存的交易所時間與接收時間，避免把實盤拒絕的行情當成可用資料。
- 上述保護已用 mock 測試，未在正式帳戶驗證；不保證成交、強制平倉成功、最大虧損上限或策略獲利。

- 目前策略本身仍是從研究規則搬到 live runtime；「可以下單」不等於已證明有正期望值。
- SHORT 不自動猜 `StockOrderType`。
- 任一送單結果不明會沿用 adapter 的 UNKNOWN + persistent halt，不自動重送。
- UNKNOWN／REJECTED／CANCELED／EXPIRED 的退場單會先重新對帳實際剩餘股數，再以新的 deterministic rescue intent 繼續處理；每次都寫入 CRITICAL 通知紀錄。
- 無持倉時若即時行情長時間中斷，runtime 會重建 SPARK session 並重新 reconciliation 後才繼續；有曝險時不會盲目重連送單，而會轉成退場優先。
- 行情在持倉期間長時間 stale 會 fail closed，不會用猜測價格繼續操作。

## Watchdog

runtime 每一輪都更新 `runtime/heartbeat.json`。可由另一個程序監看：

```bash
python3 -m yuanta_live_runtime_v01.main watchdog
```

heartbeat 遺失、超時或不安全停止會寫入持久 CRITICAL ledger，並嘗試沿用既有 Telegram／macOS 通知系統。
