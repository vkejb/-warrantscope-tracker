# Expanded shadow universe v01

這個模組只擴大「正式行情收集與盤後紙上重播」的候選池，不會送單，也不會改變 Stage A Top30 或 LIVE 交易池。

固定第一版規則：前一交易日收盤價不高於 190 元、成交金額至少 3,000 萬元、最多 400 檔，排除明確傳產、金融、食品與生技的官方產業分類。電機機械、綠能與官方電子產業保留；無官方產業身分的商品採 fail-closed 排除。

建立每日封存：

```bash
cd stock-strategy
PYTHONPATH=. python3 -m expanded_shadow_universe_v01.main build --date YYYYMMDD
```

每日封存包含官方 EOD 與公司主檔雜湊。即時訂閱由既有單一唯讀 Yuanta collector 執行，與 Top30 去重後每批最多 200 檔。資料另存為 `expanded_ticks` / `expanded_books`，不污染既有 Top30 封存。

安全邊界：`actual_orders=0`、`actual_fills=0`、`broker_order_calls=0`。模組沒有 broker order adapter，也沒有 LIVE 開關。
