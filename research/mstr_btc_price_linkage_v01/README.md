# MSTR 與 BTC 價格連動性研究 v01

這是與台股策略、LIVE runtime、券商 adapter、資料庫完全分離的唯讀研究模組。研究只回答價格同步關係、敏感度、領先落後與穩定性，不建立或調整交易策略。

## 資料與時間對齊

- MSTR、QQQ：Yahoo Finance Chart API 日 OHLCV、adjusted close 與 split 事件。
- BTC/USD：Bitstamp 公開 API 的 15 分鐘 OHLCV（UTC 時戳）。
- 美股交易時段：`pandas_market_calendars` 的 NYSE schedule，含 America/New_York 夏令時間、假日及提早收盤。
- BTC 在 MSTR 開盤/收盤的取值，只使用 candle end `<=` 該時點的最後一根完整 15 分鐘 K 線。開盤缺口分析因此不會使用 09:30 ET 之後的 BTC 資料。
- MSTR 2024-08-08 的 10:1 split 必須在公司行動事件中出現；程式也會檢查 split 前後沒有機械式約 -90% 報酬。

## 重跑

```bash
cd research/mstr_btc_price_linkage_v01
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python run_research.py --start 2020-01-01
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python verify_outputs.py
```

若執行環境以自簽代理攔截 HTTPS，可在明確理解風險後加 `--insecure-tls`。該設定會寫入 `results/data_quality.json`，輸出仍以 SHA-256 固定原始檔。

完整重抓 BTC 15 分鐘歷史資料需要數分鐘。後續重跑預設重用已保存原始檔；要重新抓取可加 `--refresh`。

## 交付檔

- `REPORT_zh-TW.md`：繁中研究報告，開頭直接回答五個問題。
- `data/processed/daily_aligned.csv`：逐日 point-in-time 對齊資料。
- `results/rolling_correlation.csv`、`rolling_beta.csv`
- `results/asymmetry.csv`、`lead_lag.csv`
- `results/annual_comparison.csv`、`regime_comparison.csv`
- `results/control_regression.csv`、`structural_change.csv`
- `results/intraday_lead_lag_status.csv`
- `results/data_quality.json`、`source_manifest.json`、`summary_metrics.json`

所有門檻與切分均預先固定：研究期 2020-01-01 至 2023-12-31，保留期 2024-01-01 起；rolling windows 為 20/60/120 個交易日；大波動門檻為 3%/5%；高低波動門檻只用研究期的 60 日 BTC 波動率中位數決定。
