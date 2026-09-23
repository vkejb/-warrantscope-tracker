# Intraday Paper Execution V0.1

這是獨立、可重啟的紙上委託狀態機。它不匯入元大 SDK、不登入券商、也不送出
真實委託；用途是先把正式交易最容易失控的狀況做成可重現測試。

已實作：

- 執行開關：預設 `DISABLED`；只允許切換成 `PAPER_ONLY`，設定會跨重啟保存。
- 委託狀態：已送出、已確認、部分成交、待撤、全成交、已撤與拒絕。
- 部分成交：以唯一成交編號去重，計算累計數量與加權平均價，禁止超額成交。
- 重複送單防護：相同 idempotency key 與相同內容只回傳原委託；同 key 不同內容直接拒絕。
- 重啟恢復：SQLite 使用 WAL 與 FULL synchronous；重新開啟同一資料庫即可還原委託、成交、部位與緊急停止狀態。
- 重啟對帳：本機狀態與權威快照不一致時立即啟動緊急停止，禁止繼續開倉。
- 強制撤單和平倉：先鎖定新開倉並要求取消所有未完成委託；撤單確認後才產生反向 EXIT 紙上委託。
- 緊急停止：狀態會持久化；必須所有委託終結且部位歸零後才能人工解除。

## 操作範例

以下全部只操作指定的本機 SQLite 檔案：

```bash
cd stock-strategy
python3 -m intraday_paper_execution_v01.main --db /private/tmp/paper-orders.sqlite status

python3 -m intraday_paper_execution_v01.main --db /private/tmp/paper-orders.sqlite \
  mode --set PAPER_ONLY --reason 紙上演練

python3 -m intraday_paper_execution_v01.main --db /private/tmp/paper-orders.sqlite \
  submit --key signal-20260923-3605-long --symbol 3605 --side BUY \
  --quantity 1000 --price 171.5

python3 -m intraday_paper_execution_v01.main --db /private/tmp/paper-orders.sqlite \
  emergency-stop --reason 人工停止
```

關閉紙上執行會立刻封鎖新開倉，並將所有未完成紙上委託改為待撤：

```bash
python3 -m intraday_paper_execution_v01.main --db /private/tmp/paper-orders.sqlite \
  mode --set DISABLED --reason 人工關閉
```

平倉必須先收到原委託的撤單確認，之後才可執行：

```bash
python3 -m intraday_paper_execution_v01.main --db /private/tmp/paper-orders.sqlite \
  flatten --price 3605=171.0
```

資料庫快照固定包含 `live_send_available=false`、`actual_orders=0`、
`actual_fills=0`、`broker_connections=0`。程式沒有 `LIVE` 模式；傳入該值會
直接拒絕，也不提供正式券商轉接器。
