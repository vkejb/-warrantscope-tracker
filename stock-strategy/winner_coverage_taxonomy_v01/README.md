# WINNER_COVERAGE_TAXONOMY_V0_1

這是獨立的研究與分類模組，目的不是建立可下單策略，而是以完整 eligible
`stock × signal_date` 母體回答：現有 frozen Setup 覆蓋了多少未來 Winner、仍漏掉
哪些 Winner，以及 2020–2022 的漏網 Winner 是否呈現可重複的 T-day 狀態。

最終判定只會是：

- `PROMISING_FOR_NEXT_HYPOTHESIS`
- `DESCRIPTIVE_ONLY`
- `NO_STABLE_NEW_FAMILY_FOUND`

任何結果都不是 validated strategy，也不能直接 promotion 或下單。

## Frozen contract 與時間紀律

- Primary outcome：以 T+1 regular-session Open 為 entry reference，在其後 10 個
  trading sessions 內，以 Close 判斷 `+8% before -5%`；同日不可能同時觸發兩道
  Close barrier。結果同時保存 Day1/3/5/10、MFE/MAE 5D/10D，以及描述性的
  `+10% before -5%`、`+15% before -5%`。
- 2020–2022：`HISTORICAL_DISCOVERY`，唯一允許 fit taxonomy、winsor、scaling、
  centroid、radius、price/volume control bucket 的期間。
- 2023–2024：`RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`。
- 2025：`STRESS_PREVALENCE_SEEN_NOT_BLIND`。
- 2026-09 以後 prospective observations 完全排除於 discovery、threshold selection
  與 taxonomy fitting。
- `N_COMPACT_RETEST_HYPOTHESIS` 原封不動：
  `pivot_separation_sessions <= 7 and bottom_difference > 0`。本模組在執行前驗證
  frozen detector contract，並直接以既有、已發布
  `multi_setup_study_v01/signal_observations.csv` 的 `(signal_date, code, setup)` 作為
  frozen membership source；不重新詮釋已產生的歷史訊號。每一個 membership 都必須
  唯一、落在 mother sample，且各 Setup 筆數完全核對。
- 不修改 `prospective_shadow_v01`、任何 prospective ledger 或每日排程；研究前後
  ledger SHA-256 必須相同。程式不存在 broker、order 或 fill 路徑。

## Mother sample 與 causal features

母體直接重用已封存、雜湊固定的
`extension_entry_study_v01/observation_store.npz`，不先要求符合任何 Setup。
2019 只作 warm-up。Universe、價格/流動性、overnight discontinuity segment、0050
benchmark、成本與 outcome 都沿用既有研究框架。每個 feature 最晚只讀 T 日：

- Price/momentum：return 3/5/10/20/60。
- Location：prior 20/60 close high、20/60D high drawdown。
- Trend：MA 5/10/20/60 location 與 slope。
- Volatility：ATR 5/20/60、ATR5/ATR20、range compression、volatility20。
- Volume：volume ratio 5/20、prior contraction 5/20。
- Relative strength：RS 5/20/60 vs 0050。
- Structure：local high/low distance、days since high、recent breakout/retest。
- Extension：BIAS 5/10/20，只作描述，不搜尋門檻。

全市場 daily OHLCV archives 來自既有 GitHub data release，其 producer 使用 TWSE
與 TPEx 官方 EOD。舊研究依賴但本機已遺失的 2023-05-25、2025-02-06 TWSE 層，
由 `official_supplement.py` 直接解析 TWSE 官方 MI_INDEX regular-session JSON 重建；
無交易 OHLC row 只記 exclusion，不做 forward-fill。原始來源 URL、retrieval time、
raw hash、row audit 與 supplement hash 保存在 runtime provenance，並嵌入正式
`run_manifest.json`。

## Coverage 與 controls

Frozen setups：N Compact、N Retest、V Reversal、Momentum Directional、Trend
Pullback、Consolidation Breakout V2。另報告 near/above prior20 Close High 與同日
return20 top 20% 描述 cohort；它們不被稱為 validated strategy。

Coverage denominator 永遠是該時間切片全部 evaluable Winners。Overlap 僅在 Winner
上計算 pairwise intersection/Jaccard；unique coverage 是只被該 frozen Setup 捕捉的
Winner，incremental coverage 則明確以 N Compact 已存在為基準。

Taxonomy 只對 discovery 期間、未被任何主要 frozen Setup 解釋且 feature 完整的
Winner 做 robust scaling + deterministic K-means。K 僅用 unsupervised
Calinski-Harabasz criterion 從預註冊候選集合選擇，不使用未來 outcome，也不在
2023–2025 refit。Discovery centroid 的 P90 radius 一併 freeze，再對全母體指派；
因此 candidate precision 與 PF 是 centroid 狀態對所有股票日期的表現，不是只看
Winner 的同義反覆。

「下一個 detector hypothesis」的候選資格也先凍結在 discovery 判斷：Gross > 0、
PF > 1、移除 Top1% 正報酬交易後 PF 仍 > 1，且不屬於 tail-dependent。若沒有 cluster
通過，欄位必須為空，不能只因 Winner coverage 大就推薦一個廣泛市場狀態；
2023–2025 只能評估已選候選，不能替換選擇。

Feature controls 採 discovery-frozen price/volume quintiles，並在同一 signal date 內
為 Winner 選擇不重複的 non-Winner control。報告 mean/median difference、standardized
mean difference 與 probability superiority，避免只看 p-value。

## 輸出

- `mother_sample_summary.csv`、`winner_base_rate.csv`：完整 denominator。
- `setup_coverage.csv`：period/year/overall 的 precision、recall、returns、PF、MFE/MAE、
  Top1/5% removal。
- `winner_overlap_matrix.csv`、`unique_marginal_coverage.csv`：Winner overlap、Jaccard、
  unique 與 N Compact incremental coverage。
- `unexplained_winner_summary.csv`：現有主要 Setup 合集仍漏掉的 Winner。
- `feature_control_comparison.csv`：Winner state 對 matched controls 的 effect size。
- `winner_taxonomy_summary.csv`：discovery-only cluster 定義與穩定性。
- `candidate_family_summary.csv`：frozen assignment 的 period/year/overall trade quality、
  tail dependence、訊號與日期/月集中度、N Compact 合併機會數。
- `validation_summary.json`：要求問題的機器可讀摘要與唯一最終分類。
- `run_manifest.json`：config、來源、程式、母體、taxonomy、ledger 與所有正式摘要 hash。
- `runtime/observation_store.npz`：大型 observation-level 記錄，只留 local、不 commit。

## 執行

先跑測試，確認無洩漏、label/denominator/overlap、frozen fit 與安全 contract：

```bash
cd stock-strategy
python3 -B -m unittest winner_coverage_taxonomy_v01.tests.test_winner_coverage_taxonomy_v01 -v
```

正式執行需要列出 2019–2025 archives、足夠計算 2025 forward outcome 的 2026 weekly
archives，以及官方 supplement。程式拒絕覆寫任何已發布摘要，因此只能自然地形成
一次 immutable publish；完整命令與 input hashes 保存在 `run_manifest.json`。

## 限制

四位數代碼只是 historical common-stock membership proxy，沒有完整 point-in-time
security master。未調整 OHLCV 與既有 0.89–1.11 overnight discontinuity rule 不能
辨識所有除權息，公司行動處理由此受限。既有 release 中無可交易 OHLC 的 source
rows 由共用 loader 排除並留 audit；不能把理論 outcome 解讀成實際成交。
