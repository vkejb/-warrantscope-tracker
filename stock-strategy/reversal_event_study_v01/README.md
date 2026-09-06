# 台股相對低點反轉事件研究 V0.1

這是獨立於 V2.1、短期大漲事件研究與盤中 ORB 的研究模組。它只用 T 日以前的日線辨識 V 型急跌止穩與早期 N／雙底回測，固定以 T+1 Open 作進場參考，再觀察 10 個市場交易日內 Close 是否先到 +8%、且未先到 -5%。不讀權證、元大 API、帳戶或憑證，也沒有送單路徑。

## 固定規則

- 股票池與流動性：四碼普通股代理、T Close 15～500 元、前 20 日均量至少 50 萬股、`Close × Volume` 均值至少 5,000 萬元。
- 至少 60 個連續市場日且位於同一價格 segment；T+1、forward 缺日或斷點只 censor outcome，不會事後刪訊號再排名。
- `V_REVERSAL`：最近三日低點的 20 日回落至少 10%、五日跌幅至少 6%；T Close 突破 T-1 High，由低點反彈 2.5%～8%，close location 至少 0.65。
- `N_RETEST`：較早的最近五日局部低點距第二底 6～30 日，第一底回落至少 10%，兩底差 ±3%，中間反彈至少 6%，T 日再出現同一確認。N 在同日重疊時優先；這不是完整頸線突破策略。
- 每個代號、每種型態各自使用 10 市場日 causal cooldown；跨研究期間會從完整歷史重播狀態。
- 12 個價量／波動／相對強弱特徵只作 discovery quintile 與 FDR 診斷，絕不篩掉 V0.1 訊號。
- 2020–2022 discovery、2023–2024 locked validation。兩個型態使用 97.5% month-cluster CI；只有個別通過全部門檻的型態才可開啟 2025。

## 三萬元成交代理

T 日收盤先按 +3% 限價和當時可用現金固定股數，T+1 regular-session Open 加滑價後若超過限價就不成交。最多三檔、每檔最多 999 股；Close 確認 +8%、-5% 或 Day10 後，下一個有量 Open 退出。停牌仍計市場日，pending exit 保留。

Baseline 為單邊 0.1% 滑價、手續費 0.1425% 的 28 折且最低 1 元、賣出稅 0.3%；stress 為單邊 0.2% 滑價與未折手續費。一般日線 Open 不是歷史零股首筆成交，所有輸出固定 `is_actual_fill=false`。

## 執行

輸出目錄必須不存在；完整成功後才會最後寫入 `run_manifest.json`。程式會記錄所有輸入、共用 loader 與本模組實作的 SHA256。

```bash
cd stock-strategy
python3 -B -m reversal_event_study_v01.main \
  --archives /private/tmp/yearly_2019.zip /private/tmp/yearly_2020.zip \
    /private/tmp/yearly_2021.zip /private/tmp/yearly_2022.zip \
    /private/tmp/yearly_2023.zip /private/tmp/yearly_2024.zip \
    /private/tmp/yearly_2025.zip /private/tmp/weekly_2026_W01.zip \
    /private/tmp/weekly_2026_W02.zip /private/tmp/weekly_2026_W03.zip \
    /private/tmp/weekly_2026_W04.zip /private/tmp/weekly_2026_W05.zip \
  --supplements /private/tmp/v21/twse_price_supplement.csv \
  --output-dir reversal_event_study_v01/runs/full_20260906_v3
```

## 已驗證結果

2026-09-06 的第一次固定規則完整運算中，V 與 N 都 validation fail，依規格未開啟 2025。V 的三萬元 baseline 為 -12.25%；N 較接近，但仍為 -1.66%、PF 0.991，stress -23.64%。沒有共通特徵通過全部穩健門檻；最明顯但仍只屬 suggestive 的線索是 V 型確認日漲幅。完整數字與解讀見 [VERIFIED_RESULT_20260906.md](VERIFIED_RESULT_20260906.md)。
