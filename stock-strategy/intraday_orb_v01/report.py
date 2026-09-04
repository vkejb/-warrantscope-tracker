from __future__ import annotations


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.3f}%"


def build_report(
    *,
    universe_audit: dict,
    signal_audit: dict,
    summary_rows: list[dict],
    validation: dict,
    quote_rows: int,
    shadow_outcomes: int,
) -> str:
    zero_friction = {
        row["horizon"]: row for row in summary_rows if row["total_friction"] == 0
    }
    lines = [
        "# INTRADAY_ORB_V0_1 研究報告",
        "",
        "本報告只重播已凍結的盤中進場規則，未做參數最佳化。它與 V2.1 完全分離。",
        "",
        "## 執行狀態",
        "",
        f"- 盤前股票池列數：{universe_audit['selected_rows']:,}",
        f"- 盤中條件評估列數：{signal_audit['evaluation_rows']:,}",
        f"- 原始進場訊號：{signal_audit['raw_signal_rows']:,}",
        f"- 每日擇一後訊號：{signal_audit['selected_signal_rows']:,}",
        f"- 因資料缺漏而 censored 的股票日：{signal_audit['censored_universe_rows']:,}",
        f"- Censored 比例：{_pct(signal_audit['censored_universe_rate'])}",
        f"- 因任一 Top 30 資料缺漏而取消選股的交易日：{signal_audit['censored_trade_date_count']:,}",
        f"- 歷史零股報價列數：{quote_rows:,}",
        f"- Shadow 結果列數：{shadow_outcomes:,}",
        f"- 驗證：{'通過' if validation['passed'] else '失敗'}",
        f"- 結果狀態：{validation['result_state']}",
        "- 真實委託：0；真實成交：0。此程式沒有正式下單入口。",
    ]
    if signal_audit["data_issue_counts"]:
        lines.extend(["", "## 資料缺漏原因", ""])
        for reason, count in signal_audit["data_issue_counts"].items():
            lines.append(f"- `{reason}`：{count:,}")
        lines.append("")
        lines.append("有資料缺漏的股票日不會產生訊號，也不會向未來補值。")
        lines.append("")
        lines.extend(
            [
                "## Signal forward return（零摩擦）",
                "",
                "| 期間 | 樣本 | 平均 | 中位數 | 勝率 | MFE | MAE |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Signal forward return（零摩擦）",
                "",
                "| 期間 | 樣本 | 平均 | 中位數 | 勝率 | MFE | MAE |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
    for horizon in ("5m", "15m", "30m", "60m", "close"):
        row = zero_friction.get(horizon)
        if not row:
            continue
        lines.append(
            f"| {horizon} | {row['sample_size']:,} | {_pct(row['average_return'])} | "
            f"{_pct(row['median_return'])} | {_pct(row['win_rate'])} | "
            f"{_pct(row['average_mfe'])} | {_pct(row['average_mae'])} |"
        )
    lines.extend(
        [
            "",
            "## 解讀限制",
            "",
            "- Forward return 的基準是第二根確認 K 的已知收盤價，只研究訊號方向，不是假設可成交價格。",
            "- 沒有歷史零股五檔時，不能計算可實現的零股交易績效。",
            "- 即使零股最佳賣價符合條件，仍不知道排隊順位，因此只標為可能可執行，不記為成交。",
            "- 尚未凍結出場規則，所以不能把本報告稱為完整策略勝率、損益曲線或最大回撤。",
            "- 修改任何條件都必須建立新版本，不能覆寫本基準後重跑。",
            "",
        ]
    )
    return "\n".join(lines)
