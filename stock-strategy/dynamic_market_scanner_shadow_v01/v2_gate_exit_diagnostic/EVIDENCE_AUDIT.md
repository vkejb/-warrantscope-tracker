# SDK, latency and fee evidence audit (2026-11-15)

Installed SDK snapshot: `YuantaSparkAPI.dll` SHA-256
`9b1b07ab827b9f1e3204ddd2622a69b79de9a577e33f171431598fe3d3dda876`
(local file modified 2026-07-13); bundled `YSendOrder.py` SHA-256
`686c0474120d57b904149dc8a18d4194e208de8e8f65b5e78a2409031600987a`.
These identify the inspected local package, not an independently verified
vendor semantic version.

| Item | Evidence | Status / model action |
|---|---|---|
| Tick DealVol semantics | [Official Yuanta StockTick subscription contract](https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/%E8%A1%8C%E6%83%85/%E5%88%86%E6%99%82%E6%98%8E%E7%B4%B0%E8%A8%82%E9%96%B1/index.html) labels `DealVol` as 成交量 but does not state domestic share/lot unit or explicitly define single-trade versus cumulative semantics. Local `YSendOrder.py` reads the same `DealVol`. Collector `collector_main.py` copies its text unscaled to archived `deal_volume` with serial and both quote/received timestamps. | One SDK value is captured per callback, but **physical unit and single-vs-cumulative semantics remain UNKNOWN**. No ×1000 conversion is added. |
| Five-level BuyVol/SellVol | [Official Yuanta five-level contract](https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/%E8%A1%8C%E6%83%85/%E6%9C%80%E4%BD%B3%E4%BA%94%E6%AA%94%E8%A1%8C%E6%83%85%E8%A8%82%E9%96%B1/index.html) identifies each quantity by price level, not as domestic 股/張. Collector copies `BuyVol1..5` and `SellVol1..5` unchanged. Archived books contain those raw strings and `received_at`, **not exchange book time**. | Physical unit and queue depth UNKNOWN. Forward checkpoints retain raw values and unit status. Strict executable capacity remains NA; no unit inference from apparent fill plausibility. |
| Order-to-fill latency | Authorized read-only installed runtime `live-orders.sqlite` contains 9 order rows, 8 fill rows, 0 broker-result-receipt rows. Four historical BUY ENTRY rows dated 10/02, 10/06, 10/08 have created-to-filled differences 0.763, 0.777, 0.909, 0.947 seconds. Four EXIT rows range 0.678 to 2840 seconds, including an EXPIRED order with a fill record, so naive pooled mean is not representative. The schema has `created_at` and `filled_at` but not a synchronized signal-to-broker-acceptance sequence. | Historical order-created-to-fill observations only, not a verified current signal-to-fill distribution. Retain frozen BASE 1s and STRESS 5s assumptions; do not substitute market-feed latency. No account or order identifiers exported. |
| Realized commission / minimum / rounding | The same authorized SQLite `live_orders`, `live_fills` and `broker_result_receipts` schemas have no charged commission/tax columns; no authorized statement/settlement file found locally. `direction_follow_backtest.py` specifies 0.000855 commission per side, minimum NT$20, 0.0015 sell tax with ceiling rounding. | These are **research settings, not actual Yuanta account fees**. Keep proxy model unchanged. Source/condition is recorded per day; no claim of actual cost calibration. |
| Full-day timing | Source manifest has collector start/end, event counts, SHA-256; pre-open sealed pool and required expanded/0050 raw channels are separately verified. | Forward day must pass collector `FULL_SESSION` and replay audit. A market callback gap alone does not prove a transport disconnect. |

The local SDK installation PDF is a 30-page Python setup guide, but its text
could not be reliably extracted with the available PDF runtime; it was **not**
used to confirm field units. The online Yuanta pages are field contracts, not
proof of account-specific fee arrangements. No broker login/query was
performed for this audit.
