# 台股全市場 Extension / Entry Timing Study V0.1

`EXTENSION_ENTRY_STUDY_V0_1` 是獨立、窄範圍、只供研究的全市場事件研究。
它不建立交易策略、不改 `N_RETEST`／`N_COMPACT_RETEST_HYPOTHESIS`、不搜尋門檻，
也沒有券商、模擬下單或真實下單入口。

## 研究時鐘與期間

- 2020–2022：`HISTORICAL_DISCOVERY`，只用這段的全市場 T 日 feature 分布凍結
  decile 與 quintile boundary；
- 2023–2024：`RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`；
- 2025：`STRESS_PREVALENCE_SEEN_NOT_BLIND`；
- 真正新的確認仍須使用 2026-09-07 起的 prospective observations。

2023–2025 不會重估 boundary、feature、cohort 或 shape 定義。這些期間已被看過，
不得稱為 blind OOS。

## 母體與共用元件

每日母體直接呼叫 `surge_event_study_v01.features.iter_signal_dates()` 的既有
eligibility：四碼普通股代理、連續且同公司行動 segment 的 60 個市場日、T 日有量、
Close 15–500 元、prior-20 平均量至少 50 萬股、成交額代理至少 5,000 萬元。
這不是完整 point-in-time security master；未調整 OHLCV 與 0.89–1.11 隔夜切段也
無法還原所有除權息，因此結果固定標示
`PROVISIONAL_CORPORATE_ACTION_UNRESOLVED`。

母體不受 N、V、Breakout 或 Momentum Setup 限制，也不套 cooldown。只有作為對照的
frozen Momentum 與 N cohort 沿用原本十市場日 cooldown。

## Feature 定義

T 日 feature 只讀 T 及以前：

- `bias_5/10/20/60 = Close[T] / MA - 1`，MA 含 T；
- `ATR20_price` 是含 T 的 20 個 price true range 算術平均；
- `atr_adjusted_bias20/10 = (Close - MA) / ATR20_price`；
- `return_3/5/10/20 = Close[T] / Close[T-n] - 1`；
- `bias20_change_3d`、`bias10_change_3d` 是 T 值減 T-3 值；
- `distance_to_20d_high/60d_high` 使用含 T 的 trailing Close high；
- `entry_gap = Open[T+1] / Close[T] - 1` 是 T+1 才知道的執行診斷，絕不是
  T-close signal feature。它的 boundary 母體只能是 T+1 entry 可觀測樣本。

每個 feature 的 pooled 2020–2022 linear empirical quantiles 凍結一次。分桶用
half-open 語義，數值恰等於 boundary 時留在較低 bucket；重複 boundary 不抖動、不拆
ties，空 bucket 如實保留。

## 統一 Outcome 與成本

直接呼叫 `multi_setup_study_v01.outcomes.evaluate_unified_outcome()`：以 T+1 regular-
session Open 作 entry proxy，之後十個市場日用 Close 判斷 `+8% before -5%`；gross
return 是先觸及的 barrier Close，否則 Day10 Close。另保存 Day1／3／5／10、MFE5／10、
MAE5／10 與 MFE/abs(MAE)。Net 使用既有三萬元等額名義成本模型：折扣手續費、最低
1 元、賣出稅 0.3%、單邊 0.1% slippage。它不是可執行成交，也沒有 portfolio allocator。

## 固定 Conditional Cohort

- `RETURN20_TOP20PCT_SAME_DAY`：同日全市場 `return20` average-rank percentile >= 0.80；
- `NEAR_OR_ABOVE_PRIOR20_CLOSE_HIGH_WITHIN_2PCT`：既有
  `breakout_vs_prior20 >= -2%`。-2% 是本次預註冊工程定義，不是最佳值；
- `MOMENTUM_DIRECTIONAL_FROZEN_CANDIDATE`：原 Surge 四特徵等權同日百分位 Top30、
  code tie-break、十市場日 cooldown；
- `N_RETEST`：原 reversal detector 與 cooldown；
- `N_COMPACT_RETEST_HYPOTHESIS`：只在已接受的 N 上套用既有固定
  `pivot_separation_sessions <= 7 and bottom_difference > 0`，不改規則。

conditional cohort 只能使用全市場同一組 frozen boundaries，不得自行重切。

## Interaction 與統計

完整輸出 Momentum strength quintile × BIAS20 quintile，以及 BIAS20 quintile × 六個
固定 entry-gap bins。沒有從表中挑最佳格。預先指定的問句比較為：Momentum Q5 的
BIAS20 Q5 minus Q3；BIAS20 Q5 的 gap >=2% minus gap 0–1%；以及相對 BIAS20 Q3 的
difference-in-differences。

Bootstrap 以整個 signal date 或 calendar month 重抽，固定 5,000 reps，沒有 trade
IID bootstrap。為避免對大量 cell 事後解讀，CI 集中在預註冊的 Low D1–D3、Mid
D4–D7、High D8–D10 level／contrast 與兩個 interaction 問句；完整 decile/quintile
cell 只報描述性 point estimate。

倒 U 必須在 2020–2022 同時符合 Mid > Low、Mid > High、兩種 cluster CI 下限都大於
零且最高 decile 位於 D4–D7；2023–2024 與 2025 只確認同方向。資訊量排序固定使用：
三個期間最小 gross-return eta-squared，乘上 discovery profile 對兩個後期 profile
Spearman correlation 的較小非負值。這只是診斷，不是 feature 或 threshold 選擇。
跨 feature 的 shape CI 沒有拿來做策略篩選，也未經多重檢定校正；輸出固定標示
`UNADJUSTED_EXPLORATORY_NO_FEATURE_SELECTION`，不能把符合描述性 classifier 的項目
稱為已驗證 edge。

## 執行

使用專案附帶、含 NumPy 的 workspace Python：

```bash
cd stock-strategy
/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -B -m unittest discover -s extension_entry_study_v01/tests -v
```

測試全部通過後，完整研究只執行一次並拒絕覆寫任何既有輸出：

```bash
cd stock-strategy
/Users/linyunyan/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -B -m extension_entry_study_v01.main \
  --archives /private/tmp/yearly_2019.zip /private/tmp/yearly_2020.zip \
    /private/tmp/yearly_2021.zip /private/tmp/yearly_2022.zip \
    /private/tmp/yearly_2023.zip /private/tmp/yearly_2024.zip \
    /private/tmp/yearly_2025.zip /private/tmp/weekly_2026_W01.zip \
    /private/tmp/weekly_2026_W02.zip /private/tmp/weekly_2026_W03.zip \
    /private/tmp/weekly_2026_W04.zip /private/tmp/weekly_2026_W05.zip \
  --supplements /private/tmp/v21/twse_price_supplement.csv \
  --output-dir extension_entry_study_v01
```

`observation_store.npz` 保存全市場逐股票日矩陣但不進 Git；Git 只保存 boundary、
bucket、interaction、bootstrap、shape、year consistency、validation summary 與 manifest。
程式先在 staging 計算及驗證，最後才發布，且 `run_manifest.json` 永遠最後移入。
