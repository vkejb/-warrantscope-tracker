# 台股短期大漲事件研究 V0.1

這是獨立於 V2.1、舊 `research_short_surge.py` 與盤中 ORB 的研究模組。它先從全體當日可選股票研究「什麼 T 日特徵與之後快速上漲有關」，再凍結特徵方向與計分方式，最後只在較晚期間驗證。它不讀權證資料、元大 API、帳戶、憑證，也沒有下單模式。

## 固定規則

- 訊號時間：T 日收盤；特徵只能使用 T 與之前資料。
- 主事件：以 T+1 Open 為基準，Day1～Day10 任一 Close 達到 +15%。
- 描述性敏感度：10%／15%／20% × 5／10／20 個市場交易日；不得拿來事後挑策略。
- 發現期：2020–2022。16 個預先登錄特徵，各只做 Q5 對 Q1 一項主檢定，calendar-month cluster sign-flip p-value 後做 BH-FDR。
- 通過特徵同時要求：BH q ≤ 0.05、pooled tail lift ≥ 1.25、2020／2021／2022 各年方向一致且 lift ≥ 1.05、每年最少 100 日且兩端各 1,000 個可觀察標籤。
- 每個經濟 family 最多一個特徵，最多四個；用方向對齊的同日 percentile 等權平均，固定選 Top30，score 同分以代號升冪。
- 驗證期：2023–2024。Top30 的 controls 是同日可計分但未入 Top30 的股票；全部驗證門檻同時通過才開啟 2025。
- 2025 曾在先前研究看過整體事件盛行率，因此即使開啟也只稱 feature-OOS／prevalence-seen，不是完全盲測。

排名不會先查看 T+1 是否成交或未來資料是否完整。程式先完成當天橫斷面排名，再把 `entry_observed`、`outcome_evaluable`、缺日、停牌或疑似公司行動分開標記。依未來結果挑出的去重事件只作描述，主要推論使用每日聚合與月叢集 bootstrap；另有不看結果的同代號 10 市場日 cooldown 敏感度。

## 三萬元資金代理

資金測試與 signal edge 分開判斷。T 日收盤後假設預掛最高為訊號價 +3% 的限價單，股數在 T 日就用該限價與費用先算好；若 T+1 的日線 Open 加滑價高於限價則不成交。停利／停損都由 Close 確認，下一個有量的 Open 才退出。最多三檔、每檔最多 999 股。

所有交易均標示 `is_actual_fill=false`。一般市場日線 Open 不是可驗證的歷史零股首筆成交，程式也未模擬委託簿、漲跌停排隊或部分成交。

## 資料限制

- 年度 OHLCV 是未還原資料；0.89～1.11 的隔夜斷點只能抓較大的公司行動，不能完整辨識小幅除權息。因此結果固定標示 `PROVISIONAL_CORPORATE_ACTION_UNRESOLVED`。
- 四碼且首碼 1～9 只是普通股的代理，缺完整 point-in-time 證券主檔。
- 流動性使用 `Close × Volume`，不是交易所實際成交金額。
- 0050 只作相對強弱與價格基準。交易日曆用全市場日期聯集，避免 0050 在 2025 分割停牌時壓縮市場交易日。
- 2023-05-25、2025-02-06 必須加入官方 TWSE 補檔，否則當天上市股票會整批缺漏。

## 執行

輸出目錄必須不存在，完整成功後才會寫 `run_manifest.json`：

```bash
cd stock-strategy
python3 -B -m surge_event_study_v01.main \
  --archives /private/tmp/yearly_2019.zip /private/tmp/yearly_2020.zip \
    /private/tmp/yearly_2021.zip /private/tmp/yearly_2022.zip \
    /private/tmp/yearly_2023.zip /private/tmp/yearly_2024.zip \
    /private/tmp/yearly_2025.zip /private/tmp/weekly_2026_W01.zip \
    /private/tmp/weekly_2026_W02.zip /private/tmp/weekly_2026_W03.zip \
    /private/tmp/weekly_2026_W04.zip /private/tmp/weekly_2026_W05.zip \
  --supplements /private/tmp/v21/twse_price_supplement.csv \
  --output-dir surge_event_study_v01/runs/full_20260906
```

關鍵輸出包括發現期五分位分析、固定特徵規則、固定 3×3 事件矩陣、Top30 訊號、月叢集 bootstrap、2023–2024 驗證與三萬元 baseline／壓力成本資金代理。若驗證失敗，輸出會明確記錄失敗，且不產生任何 2025 結果。

## 已驗證結果

2026-09-06 的第一次固定規則完整運算結果為：訊號 validation fail、三萬元 execution proxy fail，依門檻未開啟 2025。詳細數字與解讀見 [VERIFIED_RESULT_20260906.md](VERIFIED_RESULT_20260906.md)。其中 `prior_volume_contraction_5_20` 是預註冊欄位名稱，公式為 T 前 5 日均量除以更早 20 日均量；本次 HIGH 方向實際代表事前量能擴張，不應解讀成量縮。
