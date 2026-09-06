from __future__ import annotations

import math

from .config import CFG, Config


def _pct(value) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float) and not math.isfinite(value):
        return "∞" if value > 0 else "NA"
    return f"{value * 100:.2f}%"


def _num(value, digits: int = 3) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float) and not math.isfinite(value):
        return "∞" if value > 0 else "NA"
    return f"{value:.{digits}f}"


def build_report(
    *,
    data_audit: dict,
    discovery: dict,
    validation: dict,
    validation_portfolio: dict | None,
    execution_decision: dict | None,
    oos: dict | None,
    oos_portfolio: dict | None,
    run_validation: dict,
    cfg: Config = CFG,
) -> str:
    selected = discovery["selected_rule"].get("features", [])
    lines = [
        "# 台股短期大漲事件研究 V0.1",
        "",
        f"狀態：`{cfg.result_status}`。這是研究與日線成交代理，不是實際零股成交或獲利保證。",
        "",
        "## 固定研究問題",
        "",
        "- T 日收盤後只用 T 與過去資料計算特徵，T+1 Open 作為研究基準。",
        "- 主標籤：T+1 Open 起算，未來 10 個市場交易日任一 Close 達 +15%。",
        "- 固定敏感度：10%／15%／20% × 5／10／20 日；不拿敏感度結果挑規則。",
        "- 2020–2022 發現；2023–2024 鎖定驗證；只有驗證全數通過才讀取 2025 特徵 OOS。",
        "- Top30 先由所有當日 signal-eligible 股票排名，再事後標記 T+1 與 forward outcome 是否可觀察。",
        "",
        "## 資料稽核",
        "",
        f"- 四碼代號數：{data_audit.get('ordinary_code_count', 'NA')}；有效列數：{data_audit.get('ordinary_row_count', 'NA')}。",
        f"- 跳空斷點數：{data_audit.get('detected_overnight_discontinuities', 'NA')}。",
        f"- 廣泛來源缺口日期：{len(data_audit.get('broad_source_gap_dates', []))}。",
        "- `close × volume` 只是成交金額代理；四碼規則不是完整歷史普通股名冊。",
        "- 0.89–1.11 斷點只能抓到較大的公司行動，無法完整處理小幅除權息。",
        "",
        "## 發現期共通特徵",
        "",
    ]
    if selected:
        for row in selected:
            detail = next(
                item
                for item in discovery["selection_rows"]
                if item["feature"] == row["feature"]
            )
            lines.append(
                f"- `{row['feature']}`（{row['family']}，{row['direction']}）："
                f"pooled tail lift {_num(detail['pooled_lift'])}，"
                f"最差年度 {_num(detail['minimum_yearly_lift'])}，BH q={_num(detail['bh_qvalue'], 4)}。"
            )
    else:
        lines.append("沒有任何特徵同時通過 BH-FDR、pooled lift、逐年方向與最低樣本門檻；不強行建立策略。")
    lines.extend(["", "## 2023–2024 鎖定驗證", ""])
    if validation.get("status") in {"PASS", "FAIL"}:
        pooled = validation["pooled"]
        lines.extend(
            [
                f"- 結果：`{validation['status']}`。",
                f"- Top30 命中率：{_pct(pooled['selected']['event_rate'])}；同日未選 controls：{_pct(pooled['controls_excluding_top30']['event_rate'])}；lift {_num(pooled['hit_rate_lift'])}。",
                f"- Day10 Close 平均報酬差：{_pct(pooled['day10_return_increment'])}。",
                f"- Hit lift 95% CI：{_num(validation['bootstrap']['hit_rate_lift_ci95'][0])} ～ {_num(validation['bootstrap']['hit_rate_lift_ci95'][1])}。",
                f"- 報酬差 95% CI：{_pct(validation['bootstrap']['day10_return_increment_ci95'][0])} ～ {_pct(validation['bootstrap']['day10_return_increment_ci95'][1])}。",
                "- 驗證門檻："
                + "；".join(
                    f"{name}={'PASS' if passed else 'FAIL'}"
                    for name, passed in validation["gates"].items()
                )
                + "。",
            ]
        )
    else:
        lines.append(f"未執行：`{validation.get('status', 'UNKNOWN')}`。")
    lines.extend(["", "## 三萬元零股日線成交代理", ""])
    if validation_portfolio:
        base = validation_portfolio["baseline"]["summary"]
        stress = validation_portfolio["stress"]["summary"]
        lines.extend(
            [
                f"- Baseline MTM 總報酬：{_pct(base['total_return_mark_to_market'])}；完成交易 {base['completed_trades']}；勝率 {_pct(base['trade_win_rate'])}；PF {_num(base['profit_factor'])}；最大回撤 {_pct(base['maximum_drawdown'])}。",
                f"- 壓力成本總報酬：{_pct(stress['total_return_mark_to_market'])}。",
                f"- 執行代理判定：`{execution_decision['status'] if execution_decision else 'NA'}`。",
                "- 買入數量在 T 收盤以 +3% 限價與費用先算好；T+1 Open 僅是代理且 `is_actual_fill=false`。",
            ]
        )
    else:
        lines.append("沒有通過發現期特徵規則，因此未建立資金組合代理。")
    lines.extend(["", "## 2025 特徵 OOS", ""])
    if oos is None:
        lines.append("2023–2024 未全數通過，依事前規則未開啟 2025。")
    else:
        lines.append(
            f"結果：`{oos['status']}`；hit lift {_num(oos['pooled']['hit_rate_lift'])}；"
            f"Day10 報酬差 {_pct(oos['pooled']['day10_return_increment'])}。"
        )
        lines.append(cfg.oos_disclosure)
        if oos_portfolio:
            lines.append(
                f"2025 baseline 資金代理總報酬：{_pct(oos_portfolio['baseline']['summary']['total_return_mark_to_market'])}。"
            )
    lines.extend(
        [
            "",
            "## 結論界線",
            "",
            f"- 研究管線完整性：`{'PASS' if run_validation['passed'] else 'FAIL'}`。",
            "- Signal edge 與三萬元執行代理分開判斷；即使前者通過，也不代表可取得日線 Open 或零股成交。",
            "- 任何未來策略版本都必須另立版本、重新預註冊；本研究不因結果修改 V0.1。",
        ]
    )
    return "\n".join(lines) + "\n"
