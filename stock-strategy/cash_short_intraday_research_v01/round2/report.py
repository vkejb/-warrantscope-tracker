"""Write an evidence-labeled Traditional Chinese report from existing outputs.

No market source, strategy, simulator, broker, or SDK is imported. Missing
metrics remain NA. This is a presentation step, never a parameter-selection or
performance-calculation entrypoint.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path


BASE = "BASE_ASSUMED_LOTS"
WORSE = "WORSE_DELAY_SLIP"
FILTER_NAMES = {
    "R0": "原S1_NONE", "R1": "0050市場偏弱", "R2": "RS300≤−0.002",
    "R3": "VWAP已下彎180秒", "R4": "距回測位置≤0.3%", "R5": "R1且R2",
}


def fmt(value, percent=False):
    if value is None:
        return "NA"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return "NA"
        if percent:
            return f"{value * 100:+.2f}%"
        if float(value).is_integer():
            return f"{value:,.0f}"
        return f"{value:,.2f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).replace("|", "\\|").replace("\n", " ")


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def precise(value):
    """Keep weighted fill prices and small VWAP deltas legible, not rounded to zero."""
    return format(value, ".12g") if _numeric(value) else fmt(value)


def _pick(row, *keys):
    for key in keys:
        if key in row:
            return row[key]
    return None


def _rows(tables, key):
    result = tables.get(key, [])
    return [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []


def _table(lines, headers, rows):
    lines += ["|" + "|".join(headers) + "|", "|" + "|".join("---" for _ in headers) + "|"]
    materialized = list(rows)
    if not materialized:
        lines.append("|" + "|".join(["未確認／無該組紀錄", *["NA"] * (len(headers) - 1)]) + "|")
    for row in materialized:
        lines.append("|" + "|".join(fmt(value) for value in row) + "|")
    lines.append("")


def _experiment_rows(experiments, phase, scope=None, scenario=None):
    return [row for row in experiments if row.get("phase") == phase
            and row.get("statistics_scope", "FULL_PERIOD") == "FULL_PERIOD"
            and (scope is None or row.get("scope") == scope)
            and (scenario is None or row.get("scenario") == scenario)]


def _summary_table(lines, rows):
    _table(lines,
           ["版本", "有效候選日／交易日", "原候選／可評估／缺資料／條件拒絕", "條件通過／帳戶紀錄", "進場／完成", "稅費前", "稅費後", "最大回撤", "相對同範圍R0／E0", "相對空手"],
           ((row.get("variant"), f"{fmt(row.get('effective_days'))}／{fmt(row.get('trade_days'))}",
             "/".join(fmt(value) for value in (_pick(row, "original_s1_candidate_count", "signal_count"),
                       row.get("evaluable_signal_count"), row.get("unavailable_signal_count"), row.get("condition_rejected_count"))),
             f"{fmt(row.get('passed_condition_count'))}／{fmt(row.get('signal_count'))}",
             f"{fmt(row.get('entries'))}／{fmt(row.get('closed_trades'))}",
             row.get("gross_pnl"), row.get("net_pnl"), row.get("max_drawdown_twd"),
             _pick(row, "incremental_net_vs_R0", "incremental_net_vs_E0"), row.get("incremental_vs_cash")) for row in rows))


def _conclusion(lines, experiments):
    own = _experiment_rows(experiments, "ENTRY", "OWN_EVALUABLE", BASE)
    common = _experiment_rows(experiments, "ENTRY", "COMMON_EVALUABLE", BASE)
    baseline = next((row for row in own if row.get("variant") == "R0"), {})
    base_net = baseline.get("net_pnl")
    positives = [row for row in own if row.get("variant") != "R0"
                 and _numeric(row.get("net_pnl")) and row["net_pnl"] > 0
                 and (row.get("entries") or 0) > 0]
    if not own:
        lines.append("進場實驗結果未確認；本報告不以缺紀錄推論沒有訊號、沒有交易或零損益。")
    elif not positives:
        lines.append("自身可評估範圍的主情境中，沒有新增條件呈現有交易且成本後正損益的版本；空手0元不是做空優勢。即使某版本少虧，仍須區分少承擔曝險、資料剔除與條件辨識能力。")
    else:
        lines.append("自身可評估範圍中出現正淨損益的條件版本：" + "、".join(row["variant"] for row in positives)
                     + "。這只是已知探索資料上的候選現象，不是未觸碰樣本外或可上線證據。")
    lines.append("")
    for row in sorted(own, key=lambda item: item.get("variant", "")):
        variant = row.get("variant")
        if variant == "R0":
            continue
        delta = row.get("incremental_net_vs_R0")
        if delta is None and _numeric(row.get("net_pnl")) and _numeric(base_net):
            delta = row["net_pnl"] - base_net
        same = next((item for item in common if item.get("variant") == variant), {})
        stress = next((item for item in _experiment_rows(experiments, "ENTRY", "OWN_EVALUABLE", WORSE)
                       if item.get("variant") == variant), {})
        label = "少虧／增益" if _numeric(delta) and delta > 0 else "未改善" if _numeric(delta) else "改善未確認"
        lines.append(f"- {variant}（{FILTER_NAMES.get(variant, variant)}）：{label}{fmt(delta)}元；自身淨損益{fmt(row.get('net_pnl'))}元、{fmt(row.get('entries'))}筆進場（R0 {fmt(baseline.get('entries'))}筆）。共同可評估範圍相對R0 {fmt(same.get('incremental_net_vs_R0'))}元；較差成交淨損益{fmt(stress.get('net_pnl'))}元。擋掉原R0獲利／虧損成交{fmt(row.get('blocked_original_wins'))}／{fmt(row.get('blocked_original_losses'))}筆；這是配對描述，不能從舊帳戶直接相減。")
    if own:
        lines += ["", "交易變少本身不代表訊號品質改善；請同看共同範圍、被擋原獲利單、完整帳戶替代交易及空手0元。正淨損益、回撤減少與扣最佳日／筆後仍改善是不同問題，不合併成單一最佳化分數。"]
    eligible = [row.get("variant") for row in own if row.get("variant") != "R0" and row.get("selection_threshold_met") is True]
    lines += ["", "原採用門檻維持至少10筆完成、5個不同交易日且淨損益正；沒有降低門檻湊贏家。"
              + ("本輪表內達該數量／收益門檻者：" + "、".join(eligible) + "；仍全部是已知探索資料，沒有自動選用。" if eligible
                 else "目前表內沒有已確認達門檻的新增條件版本；不選贏家。"),
              "可留待新日期驗證者僅能稱候選假設：" + ("、".join(row["variant"] for row in positives) if positives else "尚無正淨損益的進場候選證據")
              + "。是否值得蒐集新日期應同看壓力情境與集中度；本次前瞻結果PENDING，未新增排程。", ""]


def _research_readout(lines, experiments, tables):
    """Interpret fixed, already-completed comparisons without choosing a model.

    Every displayed number comes from FULL_PERIOD outputs; no thresholds,
    signals, trades, or counterfactual account performance are recalculated.
    """
    def entry(variant, scope="OWN_EVALUABLE", scenario=BASE):
        return next((row for row in _experiment_rows(experiments, "ENTRY", scope, scenario)
                     if row.get("variant") == variant), {})

    def exit_row(phase, variant):
        return next((row for row in _experiment_rows(experiments, phase, scenario=BASE)
                     if row.get("variant") == variant), {})

    lines += ["### 研究解讀：改善、代價與尚未成立的證據", ""]
    r2, r2common = entry("R2"), entry("R2", "COMMON_EVALUABLE")
    r2stress, r2stresscommon = entry("R2", scenario=WORSE), entry("R2", "COMMON_EVALUABLE", WORSE)
    if r2:
        fragile = all(_numeric(row.get("net_pnl")) and row["net_pnl"] < 0 for row in (r2stress, r2stresscommon))
        lines.append(f"- R2相對弱勢：自身／共同範圍主情境淨損益{fmt(r2.get('net_pnl'))}／{fmt(r2common.get('net_pnl'))}元，進場{fmt(r2.get('entries'))}／{fmt(r2common.get('entries'))}筆；較差成交變為{fmt(r2stress.get('net_pnl'))}／{fmt(r2stresscommon.get('net_pnl'))}元。自身扣最佳日／筆後{fmt(r2.get('net_ex_best_day'))}／{fmt(r2.get('net_ex_best_trade'))}元。"
                     + ("正收益在兩種範圍都未通過壓力情境，不能稱成本後優勢穩健。" if fragile else "壓力與集中度結果須分別判讀，不能只看主情境轉正。")
                     + f"代價是擋掉原R0 {fmt(r2.get('blocked_original_wins'))}筆獲利成交；這是原cohort診斷，不代表被擋訊號可直接重加帳戶損益。")
    r5, r5stress = entry("R5"), entry("R5", scenario=WORSE)
    if r5:
        lines.append(f"- R5市場且個股弱勢：主／壓力情境{fmt(r5.get('net_pnl'))}／{fmt(r5stress.get('net_pnl'))}元，但主情境只有{fmt(r5.get('entries'))}筆、{fmt(r5.get('trade_days'))}個交易日；扣最佳日／筆後{fmt(r5.get('net_ex_best_day'))}／{fmt(r5.get('net_ex_best_trade'))}元。"
                     + ("收益完全依賴一筆／一天，不能憑壓力情境仍正就證明濾網有效。" if r5.get("entries") == 1 else "交易數仍須和原門檻一起看，不以少量正收益宣稱有效。")
                     + f"擋掉原R0獲利成交{fmt(r5.get('blocked_original_wins'))}筆。")
    r4 = entry("R4", "COMMON_EVALUABLE")
    if r4:
        lines.append(f"- R4更嚴防追空：共同範圍{fmt(r4.get('entries'))}筆進場、淨損益{fmt(r4.get('net_pnl'))}元。"
                     + ("這等同空手0元，不是有交易的空方優勢；" if r4.get("entries") == 0 and r4.get("net_pnl") == 0 else "應區分曝險減少與可交易優勢；")
                     + f"共同範圍亦擋掉原獲利成交{fmt(r4.get('blocked_original_wins'))}筆，不能將少虧全歸為辨識好壞能力。")
    r1 = entry("R1")
    unavailable = [row for row in _rows(tables, "blocked_pairs") if row.get("variant") == "R1"
                   and row.get("scenario") == BASE and row.get("evaluable") is False
                   and any(str(reason).startswith("BENCHMARK_") for reason in row.get("reason", []))]
    unique = {row.get("signal_id"): row for row in unavailable}
    losses = [row for row in unique.values() if _numeric(row.get("round1_R0_original_net_pnl"))
              and row["round1_R0_original_net_pnl"] < 0]
    if r1:
        excluded_loss = sum(row["round1_R0_original_net_pnl"] for row in losses)
        lines.append(f"- R1市場偏弱：自身主情境仍為{fmt(r1.get('net_pnl'))}元，條件已知拒絕的原獲利成交{fmt(r1.get('blocked_original_wins'))}筆。另有{len(losses)}筆原虧損單、合計{fmt(excluded_loss)}元是0050資料不足而UNAVAILABLE，不是市場濾網判對弱勢；不能把這筆缺資料剔除效果歸功於R1，也不能當成R1可實現的增益。")
    r3, r3common, r0common = entry("R3"), entry("R3", "COMMON_EVALUABLE"), entry("R0", "COMMON_EVALUABLE")
    if r3:
        def signature(variant):
            return sorted(tuple(str(row.get(key)) for key in ("day", "symbol", "signal_id", "quantity", "entry_time", "entry_price", "exit_time", "exit_price", "net_pnl"))
                          for row in _rows(tables, "trades") if row.get("phase") == "ENTRY"
                          and row.get("scope") == "COMMON_EVALUABLE" and row.get("scenario") == BASE and row.get("variant") == variant)
        same_trades = signature("R3") == signature("R0") and bool(signature("R0"))
        own_delta = r3.get("incremental_net_vs_R0")
        lines.append(f"- R3 VWAP下彎：共同範圍R3／R0淨損益{fmt(r3common.get('net_pnl'))}／{fmt(r0common.get('net_pnl'))}元，"
                     + ("逐筆signal_id／數量／進出場價時與淨損益亦相同；" if same_trades else "逐筆是否相同另看trades，不能只由總額判斷；")
                     + f"自身範圍相對R0 {fmt(own_delta)}元。"
                     + ("共同範圍沒有新增分辨效果，自身範圍反而更差，沒有有效改善證據。" if same_trades and _numeric(own_delta) and own_delta < 0 else "此結果不可自動解讀成VWAP下彎濾網有效。"))
    for variant in ("E1", "E2"):
        fixed, account = exit_row("FIXED_EXIT", variant), exit_row("ACCOUNT_EXIT", variant)
        if not fixed and not account:
            continue
        delta_fixed, delta_account = fixed.get("incremental_net_vs_R0"), account.get("incremental_net_vs_R0")
        lines.append(f"- {variant}出場：固定原{fmt(fixed.get('entries'))}筆進場淨損益{fmt(fixed.get('net_pnl'))}元、相對E0 {fmt(delta_fixed)}元；完整帳戶則{fmt(account.get('net_pnl'))}元、相對E0 {fmt(delta_account)}元。"
                     + ("同批交易少虧沒有轉成完整資金帳戶改善；較早出場造成的權益／可負擔性及後續入場差異要另看，不能選擇只報固定配對。" if _numeric(delta_fixed) and delta_fixed > 0 and _numeric(delta_account) and delta_account < 0 else "同批配對與帳戶路徑必須分開，不相加、不互相替代。"))
    lines += ["", "目前較值得保留到新日期的只是R2相對弱勢與R5組合的固定假說及其失敗條件，不是選出的交易版本。R2需檢驗成本敏感與單日集中，R5需先累積獨立新交易；R1／R3／R4不能因相對R0少虧或零成交就稱已有交易優勢。沒有新日期結果，故前瞻驗證仍PENDING；不再衍生新門檻搜尋。", ""]


def _diagnostics(lines, diagnostics):
    lines += ["## 第一輪8筆空單：數字診斷而非因果故事", "",
              f"本次診斷紀錄{len(diagnostics)}筆；八筆預期cohort與原始signal_id以precheck.json核對。若非8筆，必須視為交付完整性未確認，而非刪除不利交易。", "",
              "以下1／3／5分鐘是進場完成後目標cursor之前最後一份新鮮ask加既定懲罰的固定原量假設估值，不是保證容量足夠的回補成交。已實際平倉者另列當時狀態，不能把假設估值當仍持倉；缺報價維持NA。", ""]
    _table(lines, ["日期／股票", "signal_id", "訊號時間", "入場時間／均價／股", "退出時間／均價", "稅費前", "佣金／稅", "稅費後", "退出原因"],
           ((f"{fmt(row.get('day'))} {fmt(row.get('symbol'))}", row.get("signal_id"),
             row.get("signal_time"), f"{fmt(row.get('entry_time'))}／{precise(row.get('entry_price'))}／{fmt(row.get('quantity'))}",
             f"{fmt(row.get('exit_time'))}／{precise(row.get('exit_price'))}", row.get("gross_pnl"),
             f"{fmt(row.get('commission'))}／{fmt(row.get('sell_tax'))}", row.get("net_pnl"), row.get("exit_reason")) for row in diagnostics))
    _table(lines, ["日期／股票", "0050弱／300秒報酬", "RS300", "VWAP距離／180秒變化", "距回測位置", "1分淨估值", "3分淨估值", "5分淨估值", "各目標已平倉？"],
           ((f"{fmt(row.get('day'))} {fmt(row.get('symbol'))}",
             f"{fmt(row.get('market_filter_pass_at_signal'))}／{fmt(row.get('benchmark_return_300s'), True)}",
             fmt(row.get("relative_strength_300s"), True),
             f"{fmt(row.get('stock_vwap_gap'), True)}／{precise(row.get('vwap_delta_180s'))}",
             fmt(row.get("entry_distance_fraction"), True),
             *[row.get(f"t_plus_{second}s_fixed_original_quantity_hypothetical_quote_net_pnl") for second in (60, 180, 300)],
             "/".join(fmt(row.get(f"t_plus_{second}s_already_closed_by_target")) for second in (60, 180, 300))) for row in diagnostics))
    _table(lines, ["日期／股票", "1分已實現／剩餘股", "3分已實現／剩餘股", "5分已實現／剩餘股", "1／3／5分實際總估值"],
           ((f"{fmt(row.get('day'))} {fmt(row.get('symbol'))}",
             *[f"{fmt(row.get(f't_plus_{second}s_actual_realized_pnl_by_target'))}／{fmt(row.get(f't_plus_{second}s_actual_remaining_quantity_by_target'))}" for second in (60, 180, 300)],
             "/".join(fmt(row.get(f"t_plus_{second}s_actual_total_marked_pnl_by_target")) for second in (60, 180, 300))) for row in diagnostics))
    _table(lines, ["日期／股票", "觀察最高淨估值／時間", "觀察最低淨估值／時間", "曾有正浮盈", "已回吐正浮盈", "peak至結束下滑", "已知不利tick金額", "進場未填／未回補股", "tick／book最大觀察間隔秒"],
           ((f"{fmt(row.get('day'))} {fmt(row.get('symbol'))}",
             f"{fmt(row.get('observed_peak_net_twd'))}／{fmt(row.get('observed_peak_net_time'))}",
             f"{fmt(row.get('observed_trough_net_twd'))}／{fmt(row.get('observed_trough_net_time'))}",
             row.get("positive_peak_net_twd"), row.get("profit_surrendered_twd"),
             row.get("drawdown_peak_to_final_twd"), row.get("known_total_extra_tick_penalty_twd"),
             f"{fmt(row.get('entry_unfilled_quantity'))}／{fmt(row.get('remaining_quantity'))}",
             f"{fmt(row.get('max_tick_observed_receipt_gap_seconds'))}／{fmt(row.get('max_book_observed_receipt_gap_seconds'))}") for row in diagnostics))
    flag_names = {
        "adverse_at_one_minute_before_fees": "1分鐘含spread／懲罰的gross quote mark<0",
        "adverse_at_one_and_three_minutes_before_fees": "1及3分鐘含spread／懲罰的gross quote mark均<0",
        "entry_distance_above_fixed_0_3pct_diagnostic": "距回測位置超過0.3%（偏晚位置代理）",
        "positive_quote_profit_then_surrendered": "有正浮盈後回吐",
        "fees_and_tax_flip_gross_profit_to_net_loss": "費稅使正毛利歸零或轉負（net≤0）",
        "vwap_not_declining_over_180s": "VWAP未下彎",
        "market_not_weak_at_entry": "市場並非偏弱",
        "relative_strength_not_below_minus_0_2pct": "個股未弱於0050至少0.20百分點",
    }
    lines += ["各原因可重疊；下列分母是實際有資料可判斷的筆數，UNKNOWN不算false。1分鐘逆向標記使用實際模型入場價與ask加懲罰的gross quote mark，已含bid／ask spread及不利tick，不是無摩擦的純價格走勢或alpha證據；不能僅憑此標記斷言訊號後股票純價格直接逆向。", ""]
    flag_rows = []
    for key, label in flag_names.items():
        values = [(row.get("cause_flags") or {}).get(key, row.get("flag_" + key)) for row in diagnostics]
        flag_rows.append((label, sum(value is True for value in values), sum(isinstance(value, bool) for value in values), sum(value is None for value in values)))
    _table(lines, ["診斷", "符合", "可判斷", "未知"], flag_rows)
    costs = [row.get("commission", 0) + row.get("sell_tax", 0) for row in diagnostics
             if _numeric(row.get("commission")) and _numeric(row.get("sell_tax"))]
    lines += [f"這批可確認佣金與稅合計{fmt(sum(costs) if costs else None)}元。已知不利tick分解與收到報價之間的變動另見逐筆欄位；它們已包含於成交價，不能再次扣帳。移除滑價會改變限價／容量／後續委託，未做獨立反事實帳戶重播，因此『總滑價造成多少虧損』仍UNKNOWN。",
              "MFE／MAE為已收到ask估值路徑，不是必能成交的最佳／最差價格；原MFE的0下限沒有實際對應時點時不捏造時間。已回吐正浮盈不超過曾有正浮盈；peak-to-final下滑另包含新增虧損。觀察callback間隔不等於證明斷線，部分進場可能受深度或IOC價位限制，不能只憑部分成交就斷言深度是唯一原因。", ""]


def write_report(output: Path):
    output = Path(output)
    tables = json.loads((output / "tables.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    precheck = json.loads((output / "precheck.json").read_text(encoding="utf-8"))
    all_experiments = _rows(tables, "experiments")
    # Split rows overlap FULL_PERIOD; never add or repeat them in primary
    # comparisons, conclusions, selection checks, or all-scenario totals.
    experiments = [row for row in all_experiments
                   if row.get("statistics_scope", "FULL_PERIOD") == "FULL_PERIOD"]
    diagnostics = _rows(tables, "round1_short_diagnostics")
    lines = ["# 現股當沖做空第二輪：空方條件與出場診斷", "",
             "## 結論與證據級別", "",
             "本輪為獨立離線SHADOW_ONLY、NO_LIVE_ORDER研究，actual_orders=0。不更動第一輪結果、正式LONG、Stage A、adapter、PROD或排程；沒有券商登入、即時訂閱或實單。研究不是為了保證找到獲利版本。",
             "所有數字均屬股／張、歷史資格、實際帳戶費率及五檔交易所時間尚未完整證實的條件情境。STRICT_EXECUTABLE損益、回撤與可執行交易數是NA，不是0；不把安全閘拒絕的零交易當真實獲利或無風險證據。",
             "原13個日期全部已被看過，本輪新增條件是已知資料上的探索診斷。原train／validation／retrospective_holdout僅保留分組標籤，不再稱未觸碰樣本外；新日期驗證PENDING，沒有新增排程。", ""]
    _conclusion(lines, experiments)
    _research_readout(lines, experiments, tables)
    drift_path = output / "external_change_receipt.json"
    if drift_path.is_file():
        drift = json.loads(drift_path.read_text(encoding="utf-8"))
        lines += ["### 完成後的共用工作目錄版本差異", "",
                  f"本研究完成於{fmt(drift.get('research_completed_at'))}，原版成功驗證於{fmt(drift.get('successful_validation_at'))}。之後共用工作目錄出現非本研究的正式LONG原碼修改；發現時258個pin中{fmt(drift.get('currently_matching'))}個仍相同，差異及雜湊見external_change_receipt.json。",
                  "沒有還原或提交該外部修改，沒有改freeze、補跑研究或把新LONG規則混入結果。後續作者層驗證嘗試在freeze檢查即安全停止；原成功測試／validation記錄保留。以下績效只屬原凍結版本，不代表現在共用工作目錄的新LONG版本；需完整重現時必須另有同版研究副本，不能覆蓋現有使用者修改。", ""]
    lines += ["## 固定比較契約", "",
              "每組各由190000元開始，每日一筆、一倉，沿用3500元風險預算與整1000股；不是每個訊號都成交。空方賣出款不增加額度，原費稅、延遲、新五檔、10%容量、限價及部分成交／未完成回補規則不變。3500元是預算及觸發參考，不保證最壞損失。",
              "R0保留原S1反彈回測狀態機與候選signal_id，新增條件只在原訊號後篩選，不讓被擋訊號重啟setup；五檔不平衡只保留原排序／診斷，不新增硬門檻。0／1內外盤是vendor proxy，並非已驗證交易所真正主動買賣方。", ""]
    _table(lines, ["版本", "僅新增的固定假設"], [
        ("R0", "原S1_NONE，E0"), ("R1", "0050低於當時VWAP且300秒報酬≤0，對應原S1_WEAK"),
        ("R2", "個股300秒報酬−0050同期間報酬≤−0.002"),
        ("R3", "VWAP(t)<VWAP(t−180秒)，兩者都在各自收到資料cursor保存"),
        ("R4", "原距回測參考位置上限由0.5%收至0.3%"), ("R5", "僅R1且R2")])
    lines += ["0050或300秒比較不足時，R1／R2／R5標UNAVAILABLE、pass=null，不是條件false或0；其他版本自身可評估範圍可能較大。COMMON_EVALUABLE取同一個S1 signal_id在六組均可評估的交集，不是所有條件都通過的交集，也不要求無關30檔全部有資料。R0亦在共同範圍重新仲裁／重播。", ""]
    for scope, title in (("OWN_EVALUABLE", "自身可評估候選範圍"), ("COMMON_EVALUABLE", "共同可評估候選範圍（公平比較）")):
        lines += [f"## 進場比較：{title}", "", "主情境BASE_ASSUMED_LOTS，單位新臺幣。有效候選日不是全部股票／全部盤中時段完整；原封存日期、候選mask及逐30秒市場readiness另列。", ""]
        _summary_table(lines, _experiment_rows(experiments, "ENTRY", scope, BASE))
    lines += ["## 成交壓力、集中度與單筆統計", "",
              "BASE假設原五檔量為張、1秒延遲、不利1tick；WORSE為3秒與不利2tick；ALTERNATE_UNIT_SHARES只改量倍率為1。三者都不是已驗證單位，不能用績效好壞反推正確單位。", ""]
    _table(lines, ["階段／範圍／版本", "情境", "完成數", "平均／中位單筆", "勝率／PF", "淨損益", "最大回撤", "扣最佳日／筆後", "最大單筆損失／最差日", "原獲利／虧損單被擋"],
           ((f"{fmt(row.get('phase'))}／{fmt(row.get('scope'))}／{fmt(row.get('variant'))}",
             row.get("scenario"), row.get("closed_trades"),
             f"{fmt(_pick(row, 'average_trade_net', 'mean_trade_net', 'mean_net_pnl'))}／{fmt(_pick(row, 'median_trade_net', 'median_net_pnl'))}",
             f"{fmt(row.get('win_rate'), True)}／{fmt(_pick(row, 'profit_factor', 'PF'))}",
             row.get("net_pnl"), row.get("max_drawdown_twd"),
             f"{fmt(row.get('net_ex_best_day'))}／{fmt(row.get('net_ex_best_trade'))}",
             f"{fmt(_pick(row, 'maximum_single_loss', 'maximum_single_trade_loss', 'worst_trade_net', 'max_single_loss'))}／{fmt(_pick(row, 'worst_day', 'worst_day_pnl'))}",
             f"{fmt(row.get('blocked_original_wins'))}／{fmt(row.get('blocked_original_losses'))}")
            for row in experiments if row.get("phase") != "CASH"))
    lines += ["沒有完成交易時勝率與PF維持NA；PF為NA也可能是無虧損分母或未平帳務不完整，須看原欄位原因。扣最佳日／筆是集中度診斷，不回頭刪除交易或調參。未完成回補保留剩量與估值限制，完整損益／回撤不可估時仍NA，不以局部observed回撤冒充完整回撤。", ""]
    _table(lines, ["階段／範圍／版本", "情境", "平均MFE／MAE（原0界）", "平均持有秒", "最大正浮盈回吐", "最大peak-to-final", "進場未填股／未平股", "部分進場數／未成交attempt", "出場原因"],
           ((f"{fmt(row.get('phase'))}／{fmt(row.get('scope'))}／{fmt(row.get('variant'))}",
             row.get("scenario"), f"{fmt(row.get('mean_mfe_twd'))}／{fmt(row.get('mean_mae_twd'))}",
             row.get("mean_holding_seconds"), row.get("max_profit_giveback_twd"), row.get("max_peak_to_final_drawdown_twd"),
             f"{fmt(row.get('entry_unfilled_quantity'))}／{fmt(row.get('cover_incomplete_quantity'))}",
             f"{fmt(row.get('partial_entry_count'))}／{fmt(row.get('unfilled_entry_attempts'))}",
             _pick(row, "exit_reason_counts", "exit_reasons")) for row in experiments if row.get("phase") != "CASH"))
    lines += ["上表max_profit_giveback_twd採第二輪正浮盈capped定義：以各交易positive_profit_surrendered_twd取最大，回吐不超過曾有正浮盈；max_peak_to_final_drawdown_twd另列從正浮盈高點至結束的下滑，可能包含新增虧損。逐筆原第一輪maximum_profit_giveback_twd保留不改，不能和新capped欄混用。原候選、條件通過、帳戶disposition列數、真正IOC嘗試與進場數是不同分母，帳戶列數不可當每筆都成交；COMMON排除與UNAVAILABLE可能重疊，不得直接相加為拒絕總數。",
              "空手僅表示可評估範圍中不開部位的0損益參考，不替資料未知時段背書。", ""]
    _summary_table(lines, _experiment_rows(experiments, "CASH"))
    split_rows = [row for row in all_experiments
                  if row.get("statistics_scope") == "SPLIT_DIAGNOSTIC"
                  and row.get("phase") == "ENTRY"
                  and row.get("scope") == "OWN_EVALUABLE" and row.get("scenario") == BASE]
    if split_rows:
        lines += ["### 原時間分組診斷（不是新的樣本外）", "",
                  "以下只列ENTRY／OWN_EVALUABLE／BASE。這些列與上方FULL_PERIOD重疊，不再加總、不另算為獨立樣本；原retrospective_holdout在本輪也是已知資料。全部phase／scope／scenario分組保留於experiments。", ""]
        _table(lines, ["版本", "原分組標籤", "完成交易", "淨損益", "最大回撤"],
               ((row.get("variant"), row.get("split"), row.get("closed_trades"), row.get("net_pnl"), row.get("max_drawdown_twd")) for row in split_rows))
    _diagnostics(lines, diagnostics)
    lines += ["## 出場：固定同批進場與完整帳戶不能混算", "",
              "FIXED_EXIT每個情境都鎖定該情境原R0 cohort、entry fills／時點／價格與requested／filled股數；E0須逐筆和第一輪相同，E1／E2只更改退出。ACCOUNT_EXIT各自完整跨日重播，提早出場改變權益與可負擔股數／後續候選，不能當固定配對的純出場效果。", "",
              "E0沿用原3500淨停損、MFE_V1、反向確認、13:20開始回補及13:25截止；E1另加進場前已知反彈高點+1合法tick突破；E2持有≥300秒、net_R<0.25且買方flow>0.10、大單方向>0。沒有將第二輪較好進場再交叉搭配所有出場。",
              "price_R=原engine依入場價／股數及費稅反解net=−3500的每股停損距離；net_R=模型淨損益／3500。兩者用途不同，未為改善績效重新定義。觸發價不是成交保證，仍須延遲、新五檔及容量；尚未回補不能假造平倉。", ""]
    for phase, title in (("FIXED_EXIT", "同批進場退出診斷"), ("ACCOUNT_EXIT", "受資金限制完整帳戶")):
        lines += [f"### {title}（主情境）", ""]
        _summary_table(lines, _experiment_rows(experiments, phase, scenario=BASE))
    lines += ["逐筆E0／E1／E2與配對增量見paired_exits；帳戶交易及fills另外保存，兩者不可相加當樣本數。第一輪原E1／E2僅10日train／validation，且有因跨日權益而新增的進場，保留於round1_references，不冒充本輪13日固定cohort。", "",
              "## A與原paper、C／D核對", "",
              f"前置核對狀態：{fmt(precheck.get('status'))}；hash失配數{len(precheck.get('hash_failures', []))}。第一輪來源commit `{fmt(precheck.get('round1_source_commit'))}`；當時完成時間 `{fmt(precheck.get('round1_completed_at'))}`。完整比對方法、原始檔SHA與signal_id見precheck.json、PRECHECK.md。", ""]
    for item in precheck.get("A_vs_original_paper", {}).get("differences", []):
        lines.append(f"- {fmt(item.get('id'))}：原paper—{fmt(item.get('old_paper'))} 本輪A—{fmt(item.get('round1'))} 影響—{fmt(item.get('effect'))}")
    lines += ["", "以上是現存原碼及封存contract核對，不保證歷史執行原碼逐位元相同；A也不是原背景paper帳戶對帳結果。", ""]
    _table(lines, ["情境", "C／D交易數", "完整trade row相同？", "共同signal_id", "C-only／D-only"],
           ((row.get("scenario"), f"{fmt(row.get('C_count'))}／{fmt(row.get('D_count'))}",
             row.get("identical_full_trade_rows_except_experiment"), row.get("same_signal_ids"),
             f"{fmt(row.get('C_only_signal_ids'))}／{fmt(row.get('D_only_signal_ids'))}")
            for row in precheck.get("C_D_actual_trade_identity", [])))
    masks = _rows(tables, "market_decision_mask")
    ready = sum(row.get("long_market_evaluable") is True for row in masks)
    unknown = sum(row.get("long_market_evaluable") is False for row in masks)
    reasons = Counter(reason for row in masks if row.get("long_market_evaluable") is False
                      for reason in row.get("unavailable_reasons", []))
    lines += ["C／D相同原因是已記錄候選中沒有相同decision的LONG與S1衝突；這不是永遠不排擠，也不是市場缺資料時已證沒有LONG。本輪沒有新增D績效。",
              f"逐30秒市場readiness紀錄{len(masks)}列：可評估{ready}，UNKNOWN {unknown}；缺值原因計數（可重疊）：{fmt(dict(reasons))}。",
              "0050不足時LONG_UNAVAILABLE≠NO_LONG。第一輪D只看當批emit結果，未另gate UNKNOWN；原S1中缺市場資料的signal_id及曾成交者見precheck。此次不將那些損益重新描述成『無多訊號時空單表現』。", "",
              "## 資料、資格與尚未解決假設", "",
              "歷史資格、策略條件與成交是三層不同問題。已知先賣後買暫停／處置／停牌不得進場；未知不升級通過。歷史TWSE快取只作保守排除線索，缺當時收到時間／券商授權，沒有TPEx或完整處置／停牌歷史時保持UNKNOWN；不以今日名單回填。",
              "原13日期僅SIMPLIFIED_CONDITIONAL，不宣稱09:00起所有股票時段完整。量單位及逐筆／快照語義、五檔交易所時間、跨通道順序、官方上下限／參考價及帳戶費率仍受第一輪限制。既有來源雜湊一致只證明重用同一份資料，不證明資料本身完整或可成交。",
              "盤中0050未知、歷史VWAP不fresh等拒絕均在evaluation_mask、market_decision_mask及signals保留，不填0、不用日終VWAP或後來行情回補。原封存日期／canonical run及source hashes見manifest/precheck；本輪未新增日期或偷偷改用latest。",
              "下一步若要提升可信度，需補實證成交量／五檔單位與逐筆語義、逐日先賣後買資格及處置／停牌生效時點、五檔交易所與接收序號／時鐘、真正委託延遲、實際帳戶費率。確認後必須另開資料修正版，所有比較組共同重跑，不能只改有利組。未獲授權不自行登入、訂閱或送單。", "",
              "## 逐筆證據、重現與交付", ""]
    inventory = {key: len(value) for key, value in tables.items() if isinstance(value, list)}
    _table(lines, ["資料表", "列數"], sorted(inventory.items()))
    lines += ["experiments包含全部六進場、三退出、空手及壓力情境，含失敗／無成交版本。trades、signals、daily_results、fills、execution_issues、blocked_pairs、paired_exits及有效mask保留逐筆依據；未成交／部分成交／回補未完成不能刪除。",
              "valuation_paths.jsonl.gz保存完整已觀察模型估值路徑，不是完整raw行情feed。交易以valuation_path_id對應path_id；不同帳戶／scope可能重用同一路徑，須先依path_id去重，並按各帳戶交易順序與starting_equity組合，可獨立重算盤中與日終回撤。不可把同一路徑在多個scope重複相加。",
              "blocked_pairs只對原R0已成交signal做配對診斷；條件釋出每日額度後會改選其他交易，實際完整績效以重新仲裁的ENTRY／ACCOUNT_EXIT為準，不能直接拿被擋虧損抵銷舊總損益。",
              f"研究版本：`{fmt(_pick(manifest, 'research_id', 'version', 'study_id', 'research_version'))}`；來源commit：`{fmt(_pick(manifest, 'source_commit', 'commit'))}`；凍結／完成時間與程式／來源SHA見run_manifest.json及freeze.json。",
              "重現命令以run_manifest.json保存的reproduction_command(s)為準（需本機同一封存來源）；壓縮包不附完整raw feed、帳戶DB、Git、帳號或憑證。", "",
              "```text", fmt(_pick(manifest, "reproduction", "reproduction_command", "reproduction_commands", "reproduce_command")), "```", "",
              "新增測試、既有測試及輸出核對記錄見tests.txt／validation.json（如未附則未確認）；測試通過證明實作契約，不證明獲利或實單可成交。私下ZIP附檔案SHA供ChatGPT接手，GitHub只放研究程式，不公開資料或結果。",
              "本輪不年化、不宣稱正期望、不用同日高度相關訊號當獨立樣本計信賴區間；沒有找到改善時就停止於六個固定條件，不自動加條件繼續搜尋。", ""]
    destination = output / "RESEARCH_REPORT.md"
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    print(write_report(parser.parse_args().output))
