# Yuanta Broker Execution V0.1

這個模組只負責把既有 execution intent 接到元大 SPARK 國內證券委託，不修改
選股、訊號、行情或風控。它與 `intraday_paper_execution_v01` 分開，避免把原本
明確禁止 LIVE 的紙上執行安全契約改壞。

## 支援範圍

- 新單：`SendStockOrder` / `TradeKind=00`
- 改量：`TradeKind=03`
- 取消：`TradeKind=04`
- 改價：`TradeKind=07`
- `SendStockOrder` 的 `OnResponse` 委託結果
- `RR_RealReport` 股票委託 `RptType=50`
- `RR_RealReport` 股票成交 `RptType=51`
- `SeqNo` 成交去重與部分成交
- `RR_RealReportMerge` 彙總狀態
- `GetRealReport` 重播當日委託／成交明細，補回重啟期間遺漏事件
- `GetRealReportMerge` 重啟／人工對帳
- `GetStoreSummary` 帳戶庫存對帳
- SQLite WAL + `synchronous=FULL` 的本機持久化
- `intent_id -> BasketNo -> broker OrderNo` 三層關聯
- 不確定送單結果時 fail closed，不自動重送

官方回報的 `OrderQty` / `OkQty` / `StockQty` 都以股數表示，因此本模組的
`ExecutionIntent.quantity` 也固定使用「股」，不做張數換算。

## 安全邊界

正式送單必須同時具備三項條件：

- `EXECUTION_MODE=LIVE`
- `ENABLE_LIVE_TRADING=YES`
- 啟動程式的 CLI 確實收到 `--live`

三項值會在啟動時被封裝成不可變的 `LiveTradingGate`；缺少任一項都不能
呼叫 `SendStockOrder`。此外，每次 process 啟動後都必須先成功完成一次
`reconcile()`，沒有提供跳過正式環境啟動對帳的選項。

```python
gate = LiveTradingGate.from_environment(cli_live=args.live)
adapter = YuantaSparkExecutionAdapter(
    api=logged_in_api,
    api_types=api_types,
    account=account,
    store=store,
    live_gate=gate,
)
adapter.reconcile()
```

只有 gate 三項條件成立且對帳通過後，才允許呼叫
`submit/cancel/modify_price/reduce_quantity`。測試使用 fake API 完成 mock 對帳，
不提供正式程式繞過對帳的參數。

帳號、PFX、憑證密碼、電子交易密碼都不寫入 repo，也不由這個 adapter 保存。
登入與 session lifecycle 可沿用現有 `yuanta_intraday_shadow_v01` 的做法。元大官方文件標示 `RR_RealReport` 與 `RR_RealReportMerge` 為「登入即訂閱」，adapter 只需掛接同一個 `OnResponse`。

若 `SendStockOrder` 拋出例外或明確回傳 `False`，本機狀態會標為 `UNKNOWN`
並啟用持久化 halt；**不會自動 retry**，避免「券商其實收到，但本機沒收到 ACK」
時重複下單。

## ExecutionIntent

`submit()` 只接受本模組的 canonical `ExecutionIntent`，不接受 signal、任意
mapping 或策略物件。這是刻意的安全邊界，避免跳過 risk/bridge：

- `intent_id`
- `symbol`
- `side`
- `quantity`
- `price`
- `price_type`、`time_in_force`、`ap_code`、`order_type`、`purpose`

```python
from decimal import Decimal
from yuanta_broker_execution_v01 import ExecutionIntent, Side

intent = ExecutionIntent(
    intent_id="signal-20260923-3605-long",
    symbol="3605",
    side=Side.BUY,
    quantity=1000,
    price=Decimal("171.5"),
)
# 正式流程只應提交 bridge_strategy_intent() 的輸出。
adapter.submit(intent)
```

### 策略轉接

禁止把策略物件直接交給 broker。`bridge_strategy_intent()` 只接受風控已核准的
intent，並明確完成：

- `stock_id` → `symbol`
- `quantity_lots × 1000` → `quantity`（股）
- `suggested_limit_price` → `price`
- `intent_type` → `ENTRY/EXIT`

放空開倉與回補必須由呼叫端明確提供經審核的元大 `StockOrderType`；bridge
不會單憑 BUY/SELL 猜測現股、融券或當沖控管種類。

## 改量語意

`reduce_quantity(client_order_id, reduce_by)` 的 `reduce_by` 明確是「減少股數」，
直接傳入元大 `TradeKind=03` 的 `OrderQty`。若要把剩餘委託全部取消，使用
`cancel()`，不要用改量模擬撤單。

## Reconciliation

```python
adapter.reconcile(timeout=20)
```

會依序取得 `GetRealReport`、`GetRealReportMerge` 與 `GetStoreSummary`，先重播明細再做委託與庫存核對。庫存依 `TradeKind` 保留多空方向：現股／融資為正，
融券／借券為負。預設採最嚴格模式；同帳戶任何未登記的人工／外部庫存都會
造成 position mismatch 並 halt。

若帳戶啟動前已有人工庫存，呼叫端必須先經使用者確認，再以
`position_baseline={"2330|0": 1000}` 傳入。key 格式是
`股票代號|TradeKind`（0 現股、3 融資、4 融券、6 借券）；單寫股票代號只會
被視為現股。不同融資券種類不會互相抵銷。對帳比較的是：

`已確認基準庫存 + adapter 實際成交淨額 = 券商庫存`

禁止由 adapter 自動把當下所有庫存標記成策略基準。

若上層另有經審核的「非策略持倉 ownership model」，才可使用
`strict_positions=False`，但仍會核對本模組管理的委託成交數。

## SDK 載入

`load_api_types(vendor_dir)` 支援元大官方 Python/.NET 8 方式，從指定目錄載入
`YuantaSparkAPI.dll`。macOS 若該目錄含 `.dotnet` 會使用它；Windows 會加入
DLL directory。載入後會在登入前以 reflection 檢查下單、回報與庫存函式的
參數數量，以及 `StockOrder` 必要欄位；SDK 版本不符時直接停止。

本模組不提供「一鍵正式下單 CLI」。正式委託必須由既有 risk/execution engine
在通過自己的風控後呼叫 adapter。

## 關閉

`close()` 會先停止接收新 callback，再以 FIFO sentinel 排空已進入 queue 的事件，
確認 worker 結束後才返回。若 10 秒內無法排空，持久化 halt 並拋出錯誤。
