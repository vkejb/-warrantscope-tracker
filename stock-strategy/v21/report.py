from __future__ import annotations


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.3f}%"


def _num(value) -> str:
    return "—" if value is None else f"{value:.3f}"


def build_report(
    overall: dict,
    yearly: list[dict],
    breakout: list[dict],
    exits: list[dict],
    audit: dict,
    signal_counts: dict,
) -> str:
    yearly_lookup = {(row["year"], row["scenario"]): row for row in yearly}
    net_slip = overall["Net 0.1% one-way slippage"]
    gross = overall["Gross"]
    net_zero = overall["Net 0% slippage"]
    lines = [
        "# 台股現股短線策略 V2.1 固定基準回測",
        "",
        "本報告只呈現指定的固定規則；未做參數最佳化、Grid Search、機器學習或事後修改門檻。",
        "",
        "## 核心結果",
        "",
        f"- 最終訊號 {signal_counts['final_signals']:,} 筆，完成交易 {signal_counts['completed_trades']:,} 筆，取消 {signal_counts['cancelled']:,} 筆，資料右界設限 {signal_counts['censored']:,} 筆，持倉期間重複訊號略過 {signal_counts['skipped_existing_position']:,} 筆。",
        f"- 取消進場包含跌破 Signal Low {audit['validation']['checks']['event_reason_counts'].get('Below Signal Low', 0):,} 筆、隔日跳空超過 5% {audit['validation']['checks']['event_reason_counts'].get('Gap >5%', 0):,} 筆、缺少真正 T+1 Open {audit['validation']['checks']['event_reason_counts'].get('Missing T+1 Open', 0):,} 筆。",
        f"- Gross：勝率 {_pct(gross['win_rate'])}，平均每筆 {_pct(gross['average_return'])}，中位數 {_pct(gross['median_return'])}，Profit Factor {_num(gross['profit_factor'])}。",
        f"- 計入手續費與證交稅、但不計滑價：勝率 {_pct(net_zero['win_rate'])}，平均每筆 {_pct(net_zero['average_return'])}，Profit Factor {_num(net_zero['profit_factor'])}。",
        f"- 再加入單邊 0.1% 滑價：勝率 {_pct(net_slip['win_rate'])}，平均每筆 {_pct(net_slip['average_return'])}，中位數 {_pct(net_slip['median_return'])}，Profit Factor {_num(net_slip['profit_factor'])}。",
        "- 固定 V2.1 的原始優勢很薄：零滑價淨值接近損益兩平，加入單邊 0.1% 滑價後轉為負期望；這不是高勝率策略，而是依靠少數大賺交易拉高平均。",
        "",
        "## 逐年結果",
        "",
        "| 年度 | 完成交易 | Gross 平均 | Net 0%滑價 | Net 0.1%滑價 | Net 0.1%勝率 | Net 0.1% PF |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for year in ("2022", "2023", "2024", "2025", "2026", "2022-2026"):
        gross_row = yearly_lookup[(year, "Gross")]
        zero_row = yearly_lookup[(year, "Net 0% slippage")]
        slip_row = yearly_lookup[(year, "Net 0.1% one-way slippage")]
        lines.append(
            f"| {year} | {slip_row['trades']:,} | {_pct(gross_row['average_return'])} | "
            f"{_pct(zero_row['average_return'])} | {_pct(slip_row['average_return'])} | "
            f"{_pct(slip_row['win_rate'])} | {_num(slip_row['profit_factor'])} |"
        )

    lines.extend(
        [
            "",
            "2026 僅統計至個股資料最後日 2026-08-28；靠近資料終點、尚未能依規則完成出場的交易不納入績效。",
            "",
            "## 突破位置（Net，單邊 0.1% 滑價）",
            "",
            "| 類型 | 樣本 | 勝率 | 平均報酬 | 中位數 | Profit Factor | 平均 MFE | 平均 MAE |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in breakout:
        if row["scenario"] != "Net 0.1% one-way slippage":
            continue
        lines.append(
            f"| {row['breakout_type']} | {row['sample_size']:,} | {_pct(row['win_rate'])} | "
            f"{_pct(row['average_return'])} | {_pct(row['median_return'])} | {_num(row['profit_factor'])} | "
            f"{_pct(row['average_mfe'])} | {_pct(row['average_mae'])} |"
        )

    lines.extend(
        [
            "",
            "Extended Breakout 在本次固定樣本中相對較強，但這只是分組觀察，不能在本輪回頭改規則或宣稱樣本外仍有效。",
            "",
            "## 出場原因（Net，單邊 0.1% 滑價）",
            "",
            "| 出場原因 | 筆數 | 勝率 | 平均報酬 | 平均持有日 | 平均 MFE | 出場後5日最高漲幅 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in exits:
        if row["scenario"] != "Net 0.1% one-way slippage":
            continue
        lines.append(
            f"| {row['exit_reason']} | {row['sample_size']:,} | {_pct(row['win_rate'])} | "
            f"{_pct(row['average_return'])} | {row['average_holding_days']:.2f} | "
            f"{_pct(row['average_mfe'])} | {_pct(row['average_post_exit_5d_max_return'])} |"
        )

    requirements = audit["institutional_requirements"]
    validation = audit["validation"]
    lines.extend(
        [
            "",
            "出場原因是交易路徑的結果，不是可直接互換的獨立策略；例如時間停損組勝率高，不代表刪除停損就會得到同樣結果。",
            "",
            "## 資料與驗證",
            "",
            f"- OHLCV：{audit['ohlcv']['row_count']:,} 筆、{audit['ohlcv']['stock_count']:,} 檔，範圍 {audit['ohlcv']['first_date']}～{audit['ohlcv']['last_date']}。",
            f"- 原始四碼股票資料有 {audit['ohlcv']['invalid_ohlcv_rows_skipped']:,} 筆無效 OHLCV 被排除，沒有用假值補齊。",
            f"- 原始資料包缺少整批上市行情的 3 日，已由證交所官方資料補回：{', '.join(audit['ohlcv']['csv_supplement_dates'])}。",
            f"- 法人需求：{requirements['pairs_available']:,}/{requirements['pairs_needed']:,} 個股票日期配對完整，缺漏 {requirements['pairs_missing']}，人工補 0 為 {requirements['synthetic_zero_fills']}。",
            f"- 驗證：{validation['checks']['signals_recomputed']:,} 個訊號與 {validation['checks']['completed_trades_replayed']:,} 筆交易逐筆重算，失敗 {validation['failure_count']}，同股重疊 {validation['checks']['same_stock_overlap_count']}。",
            "- 成本以每筆 30,000 元名目金額計算零股股數、最低手續費與取整；這只是逐筆成本基準，不是同時持倉的資金曲線。",
            "- 成本假設為手續費 0.1425% × 0.28 折扣、每邊最低 1 元、賣出證交稅 0.3%，並另測單邊 0.1% 滑價。",
            "- Volume 原始單位為股；只有比較 2,000 張門檻時除以 1,000。所有 20 日價量窗與前高都排除未來資料，訊號於 T 日收盤成立、T+1 開盤進場。",
            "",
            "## 重要限制",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in audit["limitations"])
    lines.append("")
    return "\n".join(lines)
