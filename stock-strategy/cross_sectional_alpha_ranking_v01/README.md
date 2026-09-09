# CROSS_SECTIONAL_ALPHA_RANKING_V0_1

這是獨立的研究模組，不是已驗證交易策略，也沒有任何券商、委託或成交路徑。研究問題是：每天收盤後，只用 T 日及以前資料，能否把 common-outcome 未來 10 個交易日 `net_return` 較好的股票穩定排到前面。

## Frozen scope

- 母體直接重用 `winner_coverage_taxonomy_v01/runtime/observation_store.npz` 的 704,327 筆 eligible stock-date observations；不重建第二套母體。
- `entry_gap` 直接由 `extension_entry_study_v01/observation_store.npz` 以完全相同的 stock-date key 合併。
- Primary target 是既有 common outcome：T+1 regular-session Open 進場，Close-confirmed `+8% before -5%`，皆未觸及則 Day10 Close，之後套用既有 V2.1 三萬元等額成本代理。
- 39 個 causal features 預先固定，不做自動 feature search。`bias5/10/20` 只作 multivariate context，不搜尋主觀乖離門檻。
- N Compact 定義保持 `pivot_separation_sessions <= 7 and bottom_difference > 0`，只作 descriptive overlay。
- 0050 只作 descriptive market-regime proxy，不進入 V0.1 model interaction；重用 archive 中沒有可靠 TAIEX series，因此不以替代假資料補入。

## Time discipline

- 2019：feature warm-up only。
- 2020–2022：`HISTORICAL_DISCOVERY`。
- 2023–2024：`RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS`。
- 2025：`STRESS_PREVALENCE_SEEN_NOT_BLIND`。
- 2026-09 以後完全不進 model fitting、feature selection 或 threshold selection。

Discovery walk-forward 固定為 `Train 2020 → Evaluate 2021` 與 `Train 2020–2021 → Evaluate 2022`。每個 train endpoint 再 purge 最後 10 個 signal dates，避免 forward label 跨過評估邊界。Ridge alpha 候選只有 `{0.1, 1, 10, 100}`；以兩個 discovery fold 的 mean daily Spearman IC 平均選擇，平手選較小 alpha。正式 Ridge 只在 2020–2022 fit 一次，later-period refit count 必須為 0。

## Models and preprocessing

每天在當日 eligible universe 內，所有連續值使用 average-tie percentile rank `[0, 1]`。缺值固定為中性 `0.5`，不因單一 feature 表現刪除股票或 feature。保存 local-only 的 raw 與 transformed matrices 以供稽核。

- Primary：`LINEAR_RIDGE_NET_RETURN`。
- Benchmark：`IC_WEIGHTED_LINEAR_RANK`，feature 方向與 L1-normalized weights 只由 2020–2022 daily cross-sectional Spearman IC 決定。
- Primary evaluation：Top10。Top5、Top20、Top30 只作 secondary diagnostics。
- 每天完整排名為 `RAW_RANKING`；另報固定 `10D_COOLDOWN_TRADE_PROXY`，同一股票 Top10 entry 後須相隔至少 10 個市場 sessions 才能再計一筆 proxy。

## Statistics and interpretation

Daily IC、Top10-minus-universe、Top10-minus-Bottom10、top-decile-minus-bottom-decile 分開報告。所有 spread 的 95% CI 都同時以 signal-date 與 calendar-month cluster bootstrap 5,000 reps 計算，不把每天每檔股票當 IID。

Raw Top10 是相對排序，必然每天產生候選，不等於每天可交易。score concentration 與未來 payoff 的關係只做描述，本版不設定 conviction threshold。Entry gap quintiles 亦只用 discovery boundaries 作診斷，不改模型或 entry rule。

分類只能是：

- `PROMISING_FOR_PROSPECTIVE_SHADOW`
- `DESCRIPTIVE_ONLY`
- `NO_CROSS_SECTIONAL_EDGE`

即使通過 gate，也只代表值得建立另一個獨立 prospective shadow，不能稱為 validated trading strategy。

## Run and outputs

從 `stock-strategy/` 執行：

```bash
python3 -m cross_sectional_alpha_ranking_v01.main
```

正式輸出不可覆寫；`main.py` 會在任何既有正式 artifact 存在時 fail closed。大型 observation-level ranking store 位於 `runtime/` 且不提交 Git。`run_manifest.json` 最後寫入，保存 source、input、artifact、model 與 protected-ledger hashes。

測試：

```bash
python3 -m unittest discover -s cross_sectional_alpha_ranking_v01/tests -p 'test_*.py'
```

安全不變量：`actual_orders = 0`、`actual_fills = 0`、`broker_connections = 0`。

## Published result

本次唯一正式 run 的分類是 `DESCRIPTIVE_ONLY`。Frozen Ridge（alpha 100）在 2023–2024 的 Top10 net mean 為 `+0.3888%`、net PF `1.227`、相對當日 universe spread `+0.9106%`；但 2025 net mean 降為 `-0.6760%`、net PF `0.705`、universe spread 約 `-0.0092%`。兩個 later periods 的 daily IC 都為正，但 Top10 payoff 與 Winner lift 沒有跨期穩定，因此不建立 `prospective_rank_shadow_v01`。
