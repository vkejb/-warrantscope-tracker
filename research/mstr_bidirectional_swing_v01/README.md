# MSTR 多空雙向波段研究 v01

這是獨立、離線、零下單研究。它不匯入或修改台股 LONG/SHORT、LIVE、券商 adapter、DB、baseline、HALT 或 STOP。

## 固定版本

- M0：MSTR 100% 買進持有；另報 25% 名目曝險版本供風險對照。
- M1：MSTR 自身趨勢做多。
- M2：相同對稱規則只做空。
- M3：多空按時間順序共用同一資金，不同時持有多空。
- M4：只在 M3 上加入決策收盤前已知的 BTC 過去 30 個日曆日方向確認。

規則與壓力參數全部保存在 `params.json`，執行前固定並以 SHA-256 記錄。沒有參數掃描。趨勢失效定義為：多單 close < MA20 或 MA20 <= MA60；空單完全對稱。固定停損距離為 2×ATR14。

## 資料

- 重用 `../mstr_btc_price_linkage_v01/` 已驗證的 MSTR、QQQ、BTC/USD 15 分鐘行情及 NYSE point-in-time 對齊資料。
- IBIT 由 Yahoo Finance Chart API 取得，首筆為實際掛牌日 2024-01-11，不回填掛牌前資料。
- BTC 30 日確認使用 Bitstamp BTC/USD 15 分鐘完整 candle，兩端都必須在決策收盤時可知。
- MSTR 2024-08-08 10:1 split 與資料連續性再次驗證。

## 重跑

```bash
cd research/mstr_bidirectional_swing_v01
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run_study.py
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python verify_outputs.py
```

若要重新下載 IBIT，可執行：

```bash
.venv/bin/python run_study.py --refresh-ibit --insecure-tls
```

此主機若由自簽代理攔截 HTTPS，才使用 `--insecure-tls`；狀態會寫入資料品質檔。

## 邊界

SHORT 的歷史逐日 locate、可借數量、實際 borrow fee、召回與強制回補資料沒有可靠來源，因此所有含 SHORT 的版本均為 `CONDITIONAL_SIMULATION_ONLY`，資格結論為 `INSUFFICIENT_DATA`。借券費壓力不能替代真實 locate/recall 證據。

## 交付檔

- `REPORT_zh-TW.md`：先回答六個研究問題及最終 gate。
- `results/performance.csv`：各版本、研究期、保留期、全期及 IBIT 共同期間績效。
- `results/trades.csv`、`daily_equity.csv`：逐筆交易與逐日權益。
- `results/sensitivity.csv`：成本、gap、最佳交易/年度、借券費、無法借券及召回壓力。
- `results/m3_incremental_value.csv`、`m4_incremental_value.csv`：區塊 bootstrap 增量驗證。
- `results/risk_normalized_performance.csv`：相同風險尺度比較。
- `results/residual_diagnostic.csv`：純診斷，不建立規則。
- `results/data_quality.json`、`source_manifest.json`、`parameter_hash.json`、`research_status.json`。
