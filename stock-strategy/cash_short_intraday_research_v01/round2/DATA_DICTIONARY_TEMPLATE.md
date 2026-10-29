# 第二輪資料字典

以下是原碼schema說明，不含真實研究收益。各表實際列數、完整欄位及來源SHA以完成輸出為準。NA／null表示未知、不可評估或無適用分母；不等於0。所有時間含Asia/Taipei時區。股數一律為股、交易整1000股；原始五檔量仍是SDK原生未證單位。

## 範圍與識別

|欄位|意義|
|---|---|
|phase|ENTRY固定E0進場條件；FIXED_EXIT同批進場退出；ACCOUNT_EXIT完整帳戶退出；CASH空手|
|scope|OWN_EVALUABLE各自可評估；COMMON_EVALUABLE六版本可評估性交集；FIXED_COHORT同情境原R0進場|
|variant|R0～R5或E0～E2；不可將重疊scope／phase相加當獨立交易|
|scenario|BASE_ASSUMED_LOTS、WORSE_DELAY_SLIP、ALTERNATE_UNIT_SHARES；是假設，不是實證單位|
|signal_id|沿用第一輪raw S1 identity；被條件拒絕仍保留|
|day／split|封存交易日及原分組；13日全是已看資料，沒有新未觸碰OOS|

## experiments與daily_results

original_s1_candidate_count為原S1候選全集列數；evaluable_signal_count為該scope內可判斷該條件數；passed_condition_count為條件已知通過數。signal_count沿用帳戶summarize定義，是通過條件後進帳戶的dispositions列數，**不是原候選數、不是IOC attempts、也不是成交數**。unavailable_signal_count是資料不足，condition_rejected_count是已知條件不成立；common_scope_excluded_count與unavailable可重疊，不能將這些欄位直接相加當互斥拒絕總數。未成交／最低一張超限／資格拒絕等屬另外的資金或執行層，不和條件拒絕混算。

effective_days是存在可評估候選的日期數，不保證該日全部股票／時段完整；trade_days是實際模型進場的不同日期數。entries與closed_trades不同，未平曝險仍保留。

gross_pnl含模型成交價差、未扣費稅；net_pnl扣模型佣金與賣出稅。已知不利tick已在成交價中，不能再次扣除。平均／中位／勝率／PF以相應完成交易定義為準；無完成交易時勝率／PF為NA。PF缺值不可擅自補0或無窮大。

max_drawdown_twd包含可觀察未平估值路徑；若無法完成估值，完整回撤NA，observed_partial只為已觀察下限。daily_results權益跨日承接，不把每日累積浮盈相加。incremental_net_vs_R0只對同phase／scope／scenario基準；incremental_vs_cash對空手0元，不將空手稱有交易優勢。

net_ex_best_day／net_ex_best_trade是集中度診斷，不回頭刪單或選參。blocked_original_wins／losses只計原R0實際成交cohort的配對，不代表全部被擋訊號原本會成交。

experiments.max_profit_giveback_twd是第二輪正浮盈capped值：各交易positive_profit_surrendered_twd的最大值，回吐不能超過曾有正浮盈。experiments.max_peak_to_final_drawdown_twd另取各交易peak_to_final_drawdown_twd最大，包含後續新增虧損。逐筆原maximum_profit_giveback_twd保留第一輪定義不改，不和新的正浮盈回吐欄混用。

## signals與evaluation_mask

原features保持不變；round2_features保存因果新增欄位。evaluable=false、pass=null是UNAVAILABLE；evaluable=true、pass=false是已知條件不成立。known_condition_failures可保留部分已知失敗，但另一必要條件UNKNOWN時整組仍UNAVAILABLE。

market_weak為原S1_WEAK相同tick readiness及0050 VWAP／300秒報酬判定，不額外加0050五檔門檻。rs_300=原個股stock_return_5m−原benchmark_return_5m；共享欄位與重播不符即fail-closed。

vwap_180s_ago在t−180的receipt cursor保存，非日終VWAP回算；保留當時last receipt／exchange與warmup_reason。當時不必已暖滿300秒，但需觀察到開盤、fresh且非暫停。entry_distance_fraction沿用原retest_reference，不重造回測高低或狀態機。

common_evaluable只要求六條件都有資料可判斷，**不是六條件全部通過**。同一時刻無關股票缺資料不刪除別股有效候選。

## market_decision_mask

每30秒記錄0050 market_available／long_market_evaluable與缺值原因。false在此表示市場資料無法支持LONG selector評估，**不能翻譯成已確認沒有合格LONG**。300秒基準成交時間與距target的間隔另存，不能假裝稀疏資料恰好t−300。

## round1_short_diagnostics

逐筆保留第一輪entry／exit fills、requested／filled／remaining quantity、commission、sell_tax、gross／net及原R。price_R為原含成本3500停損反解的每股價差；net_R為模型淨損益／3500，不能混用。

t_plus_60s／180s／300s以entry fill後目標cursor前最後fresh ask加既定懲罰估值。fixed_original_quantity_hypothetical_quote_*維持原股數，僅描述後續行情，不保證成交容量；actual_*依目標前實際模型回補量計已實現／未實現。already_closed_by_target=true時不能宣稱原持倉仍承擔該浮盈虧。缺新鮮報價維持NA，不採目標後資料。

observed_peak／trough是實際觀察到的淨估值極值及時間。positive_peak=max(0,observed_peak)，沒有真實0估值點時不捏造0的時間。profit_surrendered=min(positive_peak,max(0,positive_peak−final_net))；drawdown_peak_to_final另包含新增虧損。第一輪floor-zero MFE及原giveback欄保留、不覆寫。

cause_flags可重疊：1分鐘gross quote mark為負、距回測位置>0.3%代理、曾有正浮盈後回吐、費稅使正毛利歸零或轉負（gross>0且net<=0）。1分鐘gross quote mark以模型實際entry price對比目標前fresh ask加不利tick，已含bid／ask spread及模型懲罰，不是無摩擦的純價格alpha；不能單憑這個flag說訊號本身純價格直接逆向。它們是數值描述，不是已證因果；UNKNOWN不算false。既有cause key的net_loss字樣不代表排除net=0，判斷以凍結的數值條件為準。

known_*_extra_tick_penalty是固定已觀察fills相對同一book level的已知懲罰算術分解；signal-to-fill quote change另列，不能全稱可消除滑價。causal_total_slippage_effect為UNKNOWN，因移除滑價會改變IOC、股數與完整帳戶路徑。

max_*_observed_receipt_gap_seconds含診斷區間邊界，只是callback觀察間隔，不證明斷線。entry_capacity_limited表示進場未全填，但原因可能深度或IOC價位，不假定單一原因。cover_incomplete_quantity保留真實模型曝險，不捏造收盤回補。

## blocked_pairs、paired_exits、fills與execution_issues

blocked_pairs保留被擋原R0獲利／虧損signal及原因；只作固定原交易的描述，不能把其PnL直接從原帳戶扣除後當新績效。

paired_exits按**同情境**原R0 cohort鎖住原entry fills及quantity，比較E0／E1／E2；ACCOUNT_EXIT則另有資金路徑，不能混同。E0 archive parity或固定entry parity不符即停止，不能靠總損益恰好相等通過。

fills保留每次entry／cover執行時間、價、1000股倍數、order／book identity、延遲後可得性；重複回報不新增可用深度。未成交IOC、部分成交、缺quote／未知時鐘／資格及回補未完成均留execution_issues；沒有紀錄不等於沒有風險。

## valuation_paths.jsonl.gz：完整模型估值路徑

這是模型從已收到行情形成的完整已觀察估值路徑，**不是完整原始raw tick／book feed**。每筆交易以valuation_path_id連到path_id，內含各估值time／net／remaining_quantity等路徑點，並保留交易日、股票與signal_id。相同交易路徑可被多個scope／帳戶引用，先按path_id去重後再讀；不同帳戶仍須依自己的交易順序、starting_equity及已平／未平狀態組合，才能獨立重算盤中／日終最大回撤。不能把多scope引用的同一path當多份獨立損益，也不能以路徑存在聲稱保證每一估值都能成交。

## manifest與隱私

run_manifest、freeze及precheck保存原始commit／dirty狀態、SPEC／config／新增原碼及原來源hash、重用紀錄、執行／測試／重現命令與安全counter。hash一致證明相同位元，不證明市場資料完整或真實可成交。

分享ZIP僅白名單研究原碼、規格與去識別結果，排除帳戶／憑證／token／完整raw feed／DB／Git。結果不放公開GitHub；code-only發布與私下成果分離。
