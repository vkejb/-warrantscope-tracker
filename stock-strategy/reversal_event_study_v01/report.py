from __future__ import annotations

from .config import CFG, Config
from .study import PATTERNS


def _pct(value) -> str:
    return "—" if value is None else f"{100 * value:.2f}%"


def _num(value, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def build_report(
    *,
    data_audit: dict,
    discovery_lookup: dict,
    validation_lookup: dict,
    validation_bootstrap: dict,
    feature_diagnostic: dict,
    validation_decision: dict,
    portfolios: dict,
    oos_lookup: dict | None,
    integrity: dict,
    cfg: Config = CFG,
) -> str:
    lines = [
        "# REVERSAL_EVENT_STUDY_V0_1 研究報告",
        "",
        f"資料狀態：`{cfg.result_status}`；研究模式：`{cfg.execution_mode}`。",
        "",
        "## 固定問題與時序",
        "",
        "T 日收盤以前辨識 V_REVERSAL 或 N_RETEST，T+1 regular-session Open 只作進場參考。"
        "主成功條件為 Day1–Day10 的 Close 先達 +8%，且此前未先達 -5%。"
        "訊號先形成，之後才檢查未來窗是否完整。",
        "",
        "N_RETEST 是早期雙底回測確認，不宣稱已完成頸線突破。成交量、波動、影線等共通特徵只作診斷，未篩掉任何 V0.1 交易。",
        "",
        "## 樣本與資料限制",
        "",
        f"- 四碼普通股代理：{data_audit.get('ordinary_code_count', 0):,} 個；OHLCV：{data_audit.get('ordinary_row_count', 0):,} 筆。",
        f"- 偵測的隔夜斷點：{data_audit.get('detected_overnight_discontinuities', 0):,}；廣泛缺口日：{len(data_audit.get('broad_source_gap_dates', []))}。",
        "- 日線未還原、四碼代號不是完整 point-in-time 主檔，且 regular Open 不是歷史零股首筆成交。",
        "",
        "## Base pattern 結果",
        "",
        "| 期間 | 型態 | 可評估 | +8先於-5 | Day10平均 | Day10中位 | Close-rule PF |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for period, lookup in (("2020–2022 discovery", discovery_lookup), ("2023–2024 validation", validation_lookup)):
        for pattern in PATTERNS:
            item = lookup.get(pattern, {})
            lines.append(
                f"| {period} | {pattern} | {item.get('evaluable_count', 0):,} | "
                f"{_pct(item.get('success_rate'))} | {_pct(item.get('average_day10_close_return'))} | "
                f"{_pct(item.get('median_day10_close_return'))} | {_num(item.get('gross_close_rule_profit_factor'))} |"
            )
    lines.extend(["", "### Validation 信賴區間與判定", ""])
    for pattern in PATTERNS:
        boot = validation_bootstrap.get(pattern, {})
        decision = validation_decision["pattern_decisions"][pattern]
        sr = boot.get("success_rate_ci", [None, None])
        rr = boot.get("average_day10_return_ci", [None, None])
        lines.append(
            f"- **{pattern}：{decision['status']}**。成功率 97.5% month-cluster CI "
            f"{_pct(sr[0])}～{_pct(sr[1])}；Day10 平均報酬 CI {_pct(rr[0])}～{_pct(rr[1])}。"
        )
        failed = [name for name, passed in decision["gates"].items() if not passed]
        lines.append(f"  未通過門檻：{', '.join(failed) if failed else '無'}。")
    lines.extend(["", "## 三萬元日線執行代理", "", "| 型態 | 情境 | 總報酬 | 最大回撤 | 完成交易 | 勝率 | PF |", "|---|---|---:|---:|---:|---:|---:|"])
    for pattern in PATTERNS:
        for scenario in ("baseline", "stress"):
            item = portfolios[pattern][scenario]["summary"]
            lines.append(
                f"| {pattern} | {scenario} | {_pct(item.get('total_return_mark_to_market'))} | "
                f"{_pct(item.get('maximum_drawdown'))} | {item.get('completed_trades', 0):,} | "
                f"{_pct(item.get('trade_win_rate'))} | {_num(item.get('profit_factor'))} |"
            )
    lines.extend(["", "## 共通特徵診斷", ""])
    candidates = feature_diagnostic.get("research_candidates", [])
    if candidates:
        for item in candidates:
            lines.append(
                f"- {item['pattern']}：`{item['feature']}`（{item['direction']}）；僅列為下一版研究候選，未改寫本次交易。"
            )
    else:
        lines.append("沒有特徵通過事前設定的跨年度、FDR 與樣本數門檻；不可硬挑看起來最好的一組。")
    lines.extend(["", "## 2025 與結論", ""])
    allowed = validation_decision.get("patterns_allowed_into_2025", [])
    if not allowed:
        lines.append("2023–2024 沒有任何型態通過全部門檻，因此依規格未開啟 2025 訊號結果。")
    else:
        lines.append(f"只有 {', '.join(allowed)} 通過並開啟 2025 feature-OOS；它不是完全盲測。")
        if oos_lookup:
            for pattern in allowed:
                item = oos_lookup.get(pattern, {})
                lines.append(
                    f"- {pattern}：{item.get('evaluable_count', 0):,} 筆，成功率 {_pct(item.get('success_rate'))}，Day10 平均 {_pct(item.get('average_day10_close_return'))}。"
                )
    lines.extend(
        [
            "",
            f"Pipeline integrity：`{'PASS' if integrity.get('passed') else 'FAIL'}`。",
            "",
            "所有資金數字都是日線 Shadow／execution proxy，不是元大實際委託或可保證成交績效。",
        ]
    )
    return "\n".join(lines) + "\n"
