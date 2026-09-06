# 台股多 Setup 統一事件研究 V0.1

`MULTI_SETUP_STUDY_V0_1` 是獨立、固定且只供研究的比較模組。它不修改
`v21`、`reversal_event_study_v01`、`surge_event_study_v01` 或任何舊輸出，
也沒有券商連線、委託或自動下單入口。

本版的問題不是「哪個參數最好」，而是六種預先固定的候選 Setup 在同一個
entry、outcome、成本與統計框架下，是否真的具有方向性 edge。程式不執行
Grid Search、Optuna、ML、門檻搜尋或結果導向的規則修改。

## 期間與證據邊界

- 2020–2022：`HISTORICAL_DISCOVERY`
- 2023–2024：`RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`
- 2025：`STRESS_PREVALENCE_SEEN_NOT_BLIND`
- 真正新的確認只能來自 2026-09-06 之後的 prospective Shadow observations。

2023–2025 已在其他研究中被看過，因此本模組即使得到正結果，也不得把它寫成
真正 blind OOS。2025 結果不得反過來改 V0.1 規則。

## 共用資料與限制

資料只載入一次，直接重用：

- `surge_event_study_v01.data.load_ohlcv()` 與 `prepare_stocks()`；
- `PreparedStock`、市場交易日聯集與 0050 benchmark；
- 0.89–1.11 隔夜斷點及非連續日期的 segment 切分；
- `v21.backtest.net_return()` 的三萬元等額名義成本代理；
- V2.1 diagnostic 的 cluster bootstrap 與集中度工具。

原始 OHLCV 未完整還原全部除權息，公司行動切段仍是 heuristic；結果必須保持
`PROVISIONAL_CORPORATE_ACTION_UNRESOLVED`。四碼代號也只是普通股歷史母體代理，
不是完整 point-in-time security master。

## 統一 outcome

所有 Setup 在 T 日收盤形成，訊號只可使用 T 日及以前資料；T+1 regular-session
Open 只是日線 entry proxy，不是歷史零股成交。

主標籤固定為：

```text
+8% before -5% within 10 trading days, Close-confirmed
```

以 T+1 Open 為基準，Day1–Day10 每日 Close 判斷先後。觸及 +8% 前若 Close
先到 -5%，即為失敗；都未觸及則以 Day10 Close 作 timeout。`gross_return` 是
第一個主 barrier Close 的實際報酬，或 timeout 的 Day10 報酬。

另保存 Day1／3／5／10 Close Return、Close-based MFE／MAE 5D／10D、
`MFE10 / abs(MAE10)`，以及描述性的 +10%／+15% before -5%。描述性標籤不參與
Setup 選擇。

`net_return` 是同一個 Close barrier／Day10 Close 上套用的理論成本代理；它不是
看到收盤確認後仍能在該收盤價成交的宣稱。每一筆訊號各自假設三萬元等額名義
本金，精確呼叫既有 `v21.backtest.net_return()`，使用折扣手續費、最低 1 元、
賣出稅 0.3% 與單邊 0.1% 滑價。程式會核對 V2.1 成本常數，若來源值漂移便中止；
每筆彼此獨立，沒有建立新的三萬元 portfolio allocator，也沒有資金槽位競爭。

## 六個固定 Setup

### `V_REVERSAL` 與 `N_RETEST`

直接呼叫 `reversal_event_study_v01.study.build_pattern_observation()`，保留既有
N 優先處理同日重疊、幾何條件、特徵與十市場日 cooldown。沒有重新撰寫或調整
V／N 門檻。

### `N_COMPACT_RETEST_HYPOTHESIS`

它只能是已納入 `N_RETEST` 的註記子集合，不會刪除或改寫 parent N：

```text
pivot_separation_sessions <= 7
bottom_difference > 0（第二底為 higher low）
```

「confirmation 不宜過度暴衝」及「T+1 大幅 gap 可能不利」仍只保留為分桶診斷；
本版沒有搜尋或新增 +1%、TR 或 gap filter。這個假說來自已看過的 2023–2024，
只能稱 descriptive hypothesis。

### `MOMENTUM_DIRECTIONAL`

沿用 Surge V0.1 已在 2020–2022 凍結的四項等權同日百分位分數：

- `atr_ratio_14`：HIGH
- `sma20_slope_5`：HIGH
- `return_20`：HIGH
- `prior_volume_contraction_5_20`：HIGH

每日依分數取 Top30、代碼作固定 tie-breaker，再使用十市場日 cooldown。原 rule
hash 是 `03bf257904806c572f13498b16044eb21f6133e0b5579fb939492c692136aad6`。
本模組不再用「十日內曾到 +15%」作主要成功條件，而是套用共用 +8/-5 路徑。
輸出仍保存要求的全部 13 個既有 Momentum 特徵。

### `TREND_PULLBACK`

新的預註冊工程 baseline，沒有調參：

- T 日 MA20 > MA60；
- MA20[T] > MA20[T-5]；
- T 前最近 20 日內至少有一日 Close 創當時 prior-20 Close 新高；
- 該高點之後、T 之前的 Close 回檔深度介於 3%–10%（含邊界）；
- T Close > MA60；
- T Close > T-1 High。

最近新高必須至少早於 T 兩日，讓回檔在訊號前真正發生。保存回檔深度／時間、
距 MA20／MA60、前段 impulse retracement、訊號報酬／量比、相對 0050 強弱與 gap。

### `CONSOLIDATION_BREAKOUT_V2`

它不複製舊 `surge_compression` 邏輯。所有 compression window 都在 T-1 結束，
避免把突破日倒灌進盤整品質：

- prior20 `(max High - min Low) / Close[T-1] <= 12%`；
- T-1 的 MA5／MA10／MA20 spread / Close[T-1] <= 4%；
- prior ATR5 / ATR20 <= 0.80；
- Close[T] > prior20 High。

12%、4%、0.80 是新預註冊工程門檻，不是歷史最佳值。本版不再搜尋其他門檻。

## 統計

- signal edge 與 portfolio equity 完全分開；
- entry gap 使用六個固定 half-open bins；
- 每個 Setup 報原始、移除正報酬贏家 Top1% 與 Top5% 後的平均、median、PF；
- `TAIL_DEPENDENT=TRUE` 表示原本正 edge 在移除 Top1% winners 後消失；
- 報每日訊號、P90、最大日、Top5 dates share、有效訊號日，以及移除最大訊號日；
- signal-date 與 calendar-month 整群 bootstrap，各固定 5,000 reps；
- 「統計支持」固定至少要求 500 筆可評估訊號、100 個 signal-date clusters、
  12 個月份，且 2023、2024 各年方向一致、兩種 cluster CI 下限為正、移除
  Top1% winners 後仍為正；未通過時只作 descriptive；
- 本版沒有 feature discovery，所以 BH-FDR 標示 `NOT_APPLICABLE`。

## Ownership / crowding

請見 [ownership_data_schema.md](ownership_data_schema.md)。本輪固定：

```text
TDCC_OWNERSHIP_FEATURES = NOT_TESTED_DATA_UNAVAILABLE
MARGIN_SHORT_FEATURES = AVAILABLE_OFFICIAL_NOT_INGESTED
```

不以目前 TDCC snapshot 回填歷史，也不使用圖片或缺乏公開時間戳的第三方資料。

## 執行

先執行測試：

```bash
cd stock-strategy
python3 -B -m unittest discover -s multi_setup_study_v01/tests -v
```

完整研究只應對一個尚未存在輸出檔的目錄執行一次：

```bash
cd stock-strategy
python3 -B -m multi_setup_study_v01.main \
  --archives /private/tmp/yearly_2019.zip /private/tmp/yearly_2020.zip \
    /private/tmp/yearly_2021.zip /private/tmp/yearly_2022.zip \
    /private/tmp/yearly_2023.zip /private/tmp/yearly_2024.zip \
    /private/tmp/yearly_2025.zip /private/tmp/weekly_2026_W01.zip \
    /private/tmp/weekly_2026_W02.zip /private/tmp/weekly_2026_W03.zip \
    /private/tmp/weekly_2026_W04.zip /private/tmp/weekly_2026_W05.zip \
  --supplements /private/tmp/v21/twse_price_supplement.csv \
  --output-dir multi_setup_study_v01
```

程式先在暫存 staging 計算與驗證，最後才發布輸出；`run_manifest.json` 永遠最後
寫入。只有 manifest 的 `status=COMPLETE` 才代表完整結果。`signal_observations.csv`
與資料稽核留在本機且預設不版控；要求的比較摘要與 manifest 會保留在模組目錄。
