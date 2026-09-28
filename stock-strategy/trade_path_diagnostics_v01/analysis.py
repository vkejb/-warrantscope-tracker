"""Winner/loser path diagnostics without changing strategy behavior."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping

from current_indicator_stop_study_v01.analysis import (
    VARIANTS as CURRENT_VARIANTS,
    simulate_current_indicator_stop,
)
from mfe_profit_protection_study_v01.analysis import (
    ResearchTrade,
    build_independent_signal_trades,
)
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC, load_session


ANALYSIS_ID = "TRADE_PATH_DIAGNOSTICS_V0_1"
HORIZONS_MINUTES = (5, 10, 15, 30)
CURRENT_POLICY = CURRENT_VARIANTS[0]


@dataclass(frozen=True, slots=True)
class Outcome:
    status: str
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    net_pnl: float | None = None
    holding_seconds: float | None = None

    @property
    def label(self) -> str:
        if self.status != "SCORED" or self.net_pnl is None:
            return "UNSCORABLE"
        if self.net_pnl > 0:
            return "WINNER"
        if self.net_pnl < 0:
            return "LOSER"
        return "FLAT"


@dataclass(frozen=True, slots=True)
class _ObservedPoint:
    at: datetime
    exit_price: float
    projected_net_pnl: float


def _outcome(row: Mapping[str, Any]) -> Outcome:
    return Outcome(
        status=str(row["status"]),
        exit_time=(datetime.fromisoformat(str(row["exit_time"])) if row.get("exit_time") else None),
        exit_price=(float(row["exit_price"]) if row.get("exit_price") is not None else None),
        exit_reason=(str(row["exit_reason"]) if row.get("exit_reason") else None),
        net_pnl=(float(row["net_pnl"]) if row.get("net_pnl") is not None else None),
        holding_seconds=(float(row["holding_seconds"]) if row.get("holding_seconds") is not None else None),
    )


def _favorable_move_pct(side: str, entry: float, price: float) -> float:
    signed = price - entry if side == "LONG" else entry - price
    return signed / entry


def _adverse_move_pct(side: str, entry: float, price: float) -> float:
    signed = entry - price if side == "LONG" else price - entry
    return signed / entry


def _extreme_points(trade: ResearchTrade, points: Iterable[Any]) -> tuple[Any, Any]:
    # Excursion begins at zero price movement. Including entry prevents an
    # all-adverse path from being reported as a negative MFE.
    candidates = [
        _ObservedPoint(
            trade.entry_time,
            trade.entry_price,
            trade.pnl_at_price(trade.entry_price),
        ),
        *list(points),
    ]
    if trade.side == "LONG":
        return max(candidates, key=lambda point: point.exit_price), min(
            candidates, key=lambda point: point.exit_price,
        )
    return min(candidates, key=lambda point: point.exit_price), max(
        candidates, key=lambda point: point.exit_price,
    )


def analyze_trade(
    trade: ResearchTrade,
    outcome: Outcome,
    *,
    horizons: tuple[int, ...] = HORIZONS_MINUTES,
    maximum_quote_staleness_seconds: float | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Analyze the observed market path; horizon rows may extend after exit."""
    max_stale = (
        float(SPEC["maximum_tick_staleness_seconds"])
        if maximum_quote_staleness_seconds is None
        else float(maximum_quote_staleness_seconds)
    )
    held_points = [
        point for point in trade.points
        if outcome.exit_time is None or point.at <= outcome.exit_time
    ]
    if held_points:
        held_mfe, held_mae = _extreme_points(trade, held_points)
        held_mfe_pnl = trade.pnl_at_price(held_mfe.exit_price)
        held_mae_pnl = trade.pnl_at_price(held_mae.exit_price)
        held_mfe_seconds = (held_mfe.at - trade.entry_time).total_seconds()
        held_mae_seconds = (held_mae.at - trade.entry_time).total_seconds()
    else:
        held_mfe = held_mae = None
        held_mfe_pnl = held_mae_pnl = None
        held_mfe_seconds = held_mae_seconds = None
    summary = {
        "trade_id": trade.trade_id,
        "session_date": trade.session_date,
        "symbol": trade.symbol,
        "stock_name": trade.stock_name,
        "side": trade.side,
        "entry_time": trade.entry_time.isoformat(),
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "outcome": outcome.label,
        "status": outcome.status,
        "exit_time": outcome.exit_time.isoformat() if outcome.exit_time else None,
        "exit_price": outcome.exit_price,
        "exit_reason": outcome.exit_reason,
        "realized_net_pnl": outcome.net_pnl,
        "holding_seconds": outcome.holding_seconds,
        "holding_minutes": outcome.holding_seconds / 60 if outcome.holding_seconds is not None else None,
        "held_mfe_price": held_mfe.exit_price if held_mfe else None,
        "held_mfe_net_pnl": held_mfe_pnl,
        "held_mfe_move_pct": (
            _favorable_move_pct(trade.side, trade.entry_price, held_mfe.exit_price)
            if held_mfe else None
        ),
        "held_time_to_mfe_seconds": held_mfe_seconds,
        "held_mae_price": held_mae.exit_price if held_mae else None,
        "held_mae_net_pnl": held_mae_pnl,
        "held_mae_move_pct": (
            _adverse_move_pct(trade.side, trade.entry_price, held_mae.exit_price)
            if held_mae else None
        ),
        "held_time_to_mae_seconds": held_mae_seconds,
        "held_mfe_before_mae": (
            held_mfe.at <= held_mae.at if held_mfe and held_mae else None
        ),
    }
    rows: list[dict[str, Any]] = []
    for minutes in horizons:
        target = trade.entry_time + timedelta(minutes=minutes)
        through = [point for point in trade.points if point.at <= target]
        last = through[-1] if through else None
        source_reaches_target = bool(trade.points and trade.points[-1].at >= target)
        quote_staleness = (target - last.at).total_seconds() if last else None
        complete = bool(
            source_reaches_target
            and quote_staleness is not None
            and 0 <= quote_staleness <= max_stale
        )
        row = {
            "trade_id": trade.trade_id,
            "session_date": trade.session_date,
            "symbol": trade.symbol,
            "stock_name": trade.stock_name,
            "side": trade.side,
            "outcome": outcome.label,
            "horizon_minutes": minutes,
            "target_time": target.isoformat(),
            "horizon_complete": complete,
            "source_reaches_target": source_reaches_target,
            "quote_staleness_seconds": quote_staleness,
            "observations": len(through),
            "position_still_open": bool(outcome.exit_time and outcome.exit_time >= target),
            "counterfactual_after_exit": bool(outcome.exit_time and outcome.exit_time < target),
            "mark_time": last.at.isoformat() if complete and last else None,
            "mark_price": last.exit_price if complete and last else None,
            "mark_net_pnl": last.projected_net_pnl if complete and last else None,
            "mark_move_pct": (
                _favorable_move_pct(trade.side, trade.entry_price, last.exit_price)
                if complete and last else None
            ),
            "mfe_price": None,
            "mfe_net_pnl": None,
            "mfe_move_pct": None,
            "time_to_mfe_seconds": None,
            "mae_price": None,
            "mae_net_pnl": None,
            "mae_move_pct": None,
            "time_to_mae_seconds": None,
            "mfe_before_mae": None,
        }
        if complete and through:
            mfe, mae = _extreme_points(trade, through)
            row.update({
                "mfe_price": mfe.exit_price,
                "mfe_net_pnl": trade.pnl_at_price(mfe.exit_price),
                "mfe_move_pct": _favorable_move_pct(trade.side, trade.entry_price, mfe.exit_price),
                "time_to_mfe_seconds": (mfe.at - trade.entry_time).total_seconds(),
                "mae_price": mae.exit_price,
                "mae_net_pnl": trade.pnl_at_price(mae.exit_price),
                "mae_move_pct": _adverse_move_pct(trade.side, trade.entry_price, mae.exit_price),
                "time_to_mae_seconds": (mae.at - trade.entry_time).total_seconds(),
                "mfe_before_mae": mfe.at <= mae.at,
            })
        rows.append(row)
    return summary, rows


def _safe_mean(values: Iterable[float | None]) -> float | None:
    kept = [float(value) for value in values if value is not None]
    return mean(kept) if kept else None


def _safe_median(values: Iterable[float | None]) -> float | None:
    kept = [float(value) for value in values if value is not None]
    return median(kept) if kept else None


def _comparison(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for minutes in HORIZONS_MINUTES:
        for label in ("WINNER", "LOSER"):
            all_rows = [
                row for row in rows
                if row["horizon_minutes"] == minutes and row["outcome"] == label
            ]
            scored = [row for row in all_rows if row["horizon_complete"]]
            output.append({
                "horizon_minutes": minutes,
                "outcome": label,
                "total_trades": len(all_rows),
                "complete_trades": len(scored),
                "coverage_rate": len(scored) / len(all_rows) if all_rows else None,
                "average_mark_net_pnl": _safe_mean(row["mark_net_pnl"] for row in scored),
                "median_mark_net_pnl": _safe_median(row["mark_net_pnl"] for row in scored),
                "average_mark_move_pct": _safe_mean(row["mark_move_pct"] for row in scored),
                "average_mfe_net_pnl": _safe_mean(row["mfe_net_pnl"] for row in scored),
                "median_mfe_net_pnl": _safe_median(row["mfe_net_pnl"] for row in scored),
                "average_mfe_move_pct": _safe_mean(row["mfe_move_pct"] for row in scored),
                "average_mae_net_pnl": _safe_mean(row["mae_net_pnl"] for row in scored),
                "median_mae_net_pnl": _safe_median(row["mae_net_pnl"] for row in scored),
                "average_mae_move_pct": _safe_mean(row["mae_move_pct"] for row in scored),
                "average_time_to_mfe_seconds": _safe_mean(row["time_to_mfe_seconds"] for row in scored),
                "average_time_to_mae_seconds": _safe_mean(row["time_to_mae_seconds"] for row in scored),
                "profitable_at_horizon_rate": (
                    sum(float(row["mark_net_pnl"]) > 0 for row in scored) / len(scored)
                    if scored else None
                ),
                "mfe_before_mae_rate": (
                    sum(bool(row["mfe_before_mae"]) for row in scored) / len(scored)
                    if scored else None
                ),
                "already_exited_rate": (
                    sum(bool(row["counterfactual_after_exit"]) for row in scored) / len(scored)
                    if scored else None
                ),
            })
    return output


def _side_comparison(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for side in ("LONG", "SHORT"):
        selected = [row for row in rows if row["side"] == side]
        for row in _comparison(selected):
            output.append({"side": side, **row})
    return output


def _holding_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for label in ("WINNER", "LOSER"):
        selected = [row for row in rows if row["outcome"] == label]
        output.append({
            "outcome": label,
            "trades": len(selected),
            "average_holding_minutes": _safe_mean(row["holding_minutes"] for row in selected),
            "median_holding_minutes": _safe_median(row["holding_minutes"] for row in selected),
            "average_realized_net_pnl": _safe_mean(row["realized_net_pnl"] for row in selected),
            "average_held_mfe_net_pnl": _safe_mean(row["held_mfe_net_pnl"] for row in selected),
            "average_held_mae_net_pnl": _safe_mean(row["held_mae_net_pnl"] for row in selected),
            "average_time_to_mfe_minutes": (
                _safe_mean(row["held_time_to_mfe_seconds"] for row in selected) / 60
                if _safe_mean(row["held_time_to_mfe_seconds"] for row in selected) is not None else None
            ),
            "average_time_to_mae_minutes": (
                _safe_mean(row["held_time_to_mae_seconds"] for row in selected) / 60
                if _safe_mean(row["held_time_to_mae_seconds"] for row in selected) is not None else None
            ),
        })
    return output


def build_report(
    session_runs: Mapping[str, list[Path]],
    capital: int = 190_000,
) -> dict[str, Any]:
    trades, diagnostics, coverage = build_independent_signal_trades(session_runs, capital)
    stocks_by_date = {}
    for paths in session_runs.values():
        stocks, manifest = load_session(paths)
        stocks_by_date[str(manifest["session_date"])] = stocks
    trade_rows, horizon_rows = [], []
    for trade in trades:
        data = stocks_by_date[trade.session_date][trade.symbol]
        result = simulate_current_indicator_stop(trade, data, CURRENT_POLICY)
        summary, horizons = analyze_trade(trade, _outcome(result))
        trade_rows.append(summary)
        horizon_rows.extend(horizons)
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": "RETROSPECTIVE_PATH_DIAGNOSTIC_ONLY; NOT A STRATEGY RULE OR EXECUTABLE PORTFOLIO",
        "trade_universe": "FIRST_AFFORDABLE_INDEPENDENT_SIGNAL_PER_SYMBOL_PER_SESSION",
        "outcome_policy": "CURRENT_NEG_1R_WITH_CURRENT_MFE_V1_REVERSAL_AND_HARD_EXIT",
        "horizon_method": "FULL_RECORDED_MARKET_PATH_FROM_ENTRY; POST-EXIT VALUES ARE COUNTERFACTUAL; NO FORWARD FILL",
        "maximum_quote_staleness_seconds": float(SPEC["maximum_tick_staleness_seconds"]),
        "capital_twd": capital,
        "coverage": coverage,
        "source_diagnostics": diagnostics,
        "trade_summary": trade_rows,
        "horizon_rows": horizon_rows,
        "winner_loser_comparison": _comparison(horizon_rows),
        "side_comparison": _side_comparison(horizon_rows),
        "holding_summary": _holding_summary(trade_rows),
        "counts": {
            "trades": len(trade_rows),
            "scored": sum(row["status"] == "SCORED" for row in trade_rows),
            "winners": sum(row["outcome"] == "WINNER" for row in trade_rows),
            "losers": sum(row["outcome"] == "LOSER" for row in trade_rows),
            "flat": sum(row["outcome"] == "FLAT" for row in trade_rows),
            "unscorable": sum(row["outcome"] == "UNSCORABLE" for row in trade_rows),
        },
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    _write_csv(output_dir / "per_trade_summary.csv", report["trade_summary"])
    _write_csv(output_dir / "per_trade_horizons.csv", report["horizon_rows"])
    _write_csv(output_dir / "winner_loser_comparison.csv", report["winner_loser_comparison"])
    _write_csv(output_dir / "winner_loser_by_side.csv", report["side_comparison"])
    _write_csv(output_dir / "holding_time_summary.csv", report["holding_summary"])
    counts = report["counts"]
    holding = {row["outcome"]: row for row in report["holding_summary"]}
    lines = [
        "# 歷史交易 MAE / MFE / 持倉時間診斷", "",
        "本報告只做事後路徑診斷，沒有修改任何選股、進場、出場、部位或實盤設定。", "",
        "## 樣本與口徑", "",
        f"- 共 {counts['trades']} 筆不重複訊號交易；可評分 {counts['scored']} 筆（贏家 {counts['winners']}、輸家 {counts['losers']}），不可評分 {counts['unscorable']} 筆。",
        "- 贏家／輸家依目前策略 `CURRENT_NEG_1R` 的最終含成本損益分類。",
        "- 5／10／15／30 分鐘採進場後完整市場路徑；若交易已出場，之後數值只代表反事實市場走勢。",
        "- 報價超過 5 秒或資料未涵蓋時間窗即列為不完整，不補值。",
        "- 三個交易日皆為品質受限的 `PARTIAL_SESSION`；訊號可能重疊，不能視為單一 19 萬元帳戶績效。", "",
        "## 持倉時間", "",
        "| 類別 | 筆數 | 平均持倉(分) | 中位持倉(分) | 平均最終損益 | 持倉內平均 MFE | 持倉內平均 MAE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label in ("WINNER", "LOSER"):
        row = holding[label]
        lines.append(
            f"| {label} | {row['trades']} | {_fmt(row['average_holding_minutes'], 1)} | "
            f"{_fmt(row['median_holding_minutes'], 1)} | {_fmt(row['average_realized_net_pnl'])} | "
            f"{_fmt(row['average_held_mfe_net_pnl'])} | {_fmt(row['average_held_mae_net_pnl'])} |"
        )
    lines.extend([
        "", "## 進場後行為差異", "",
        "MFE／MAE 與時間窗損益均已包含既有手續費、稅與不利一檔成交代理。MAE 欄位是該窗內最差可平倉淨損益。", "",
        "| 時間 | 類別 | 完整/全部 | 平均窗末損益 | 平均 MFE | 平均 MAE | 窗末獲利率 | MFE先於MAE |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ])
    comp = report["winner_loser_comparison"]
    for row in comp:
        lines.append(
            f"| {row['horizon_minutes']}分 | {row['outcome']} | {row['complete_trades']}/{row['total_trades']} | "
            f"{_fmt(row['average_mark_net_pnl'])} | {_fmt(row['average_mfe_net_pnl'])} | "
            f"{_fmt(row['average_mae_net_pnl'])} | {_fmt_pct(row['profitable_at_horizon_rate'])} | "
            f"{_fmt_pct(row['mfe_before_mae_rate'])} |"
        )
    by_key = {(row["horizon_minutes"], row["outcome"]): row for row in comp}
    five_win, five_loss = by_key[(5, "WINNER")], by_key[(5, "LOSER")]
    ten_win, ten_loss = by_key[(10, "WINNER")], by_key[(10, "LOSER")]
    lines.extend([
        "", "## 目前樣本呈現的差異", "",
        f"- 可評分交易中，贏家持倉內平均 MFE 為 {_fmt(holding['WINNER']['average_held_mfe_net_pnl'])} 元，輸家只有 {_fmt(holding['LOSER']['average_held_mfe_net_pnl'])} 元；輸家的平均 MAE 也較深（{_fmt(holding['LOSER']['average_held_mae_net_pnl'])} 元 vs. {_fmt(holding['WINNER']['average_held_mae_net_pnl'])} 元）。",
        f"- 在資料完整的子樣本中，5 分鐘平均窗末損益為贏家 {_fmt(five_win['average_mark_net_pnl'])} 元、輸家 {_fmt(five_loss['average_mark_net_pnl'])} 元；10 分鐘為 {_fmt(ten_win['average_mark_net_pnl'])} 元 vs. {_fmt(ten_loss['average_mark_net_pnl'])} 元。差異很早出現，但 5 分鐘僅 {five_win['complete_trades']} 筆贏家與 {five_loss['complete_trades']} 筆輸家，不能當成新規則。",
        f"- 贏家的持倉內 MAE 平均在進場後 {_fmt(holding['WINNER']['average_time_to_mae_minutes'], 1)} 分鐘出現，MFE 平均到 {_fmt(holding['WINNER']['average_time_to_mfe_minutes'], 1)} 分鐘才出現；目前贏家多半不是一路直上，而是先承受早期逆行再走出趨勢。",
        f"- 輸家的平均持倉時間較長（{_fmt(holding['LOSER']['average_holding_minutes'], 1)} 分 vs. {_fmt(holding['WINNER']['average_holding_minutes'], 1)} 分），但輸家中位數只有 {_fmt(holding['LOSER']['median_holding_minutes'], 1)} 分，顯示平均值被少數長時間虧損單拉高，不能只靠單一持倉時間門檻解釋。",
        "- LONG 與 SHORT 分拆後，5／10 分鐘的贏輸方向仍大致一致，但各格只有 1–4 筆；細節已保存在 `winner_loser_by_side.csv`。",
    ])
    lines.extend([
        "", "## 解讀限制", "",
        "百分比欄位在 CSV/JSON 中以 0–1 表示；Markdown 顯示為百分比。另附依 LONG／SHORT 拆分的 CSV，避免方向組成混淆。樣本太小，不應據此新增門檻或宣稱統計顯著。",
        "本報告的目的只是找出可後續驗證的行為差異；策略維持原狀。",
    ])
    (output_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    artifact_names = (
        "summary.json", "per_trade_summary.csv", "per_trade_horizons.csv",
        "winner_loser_comparison.csv", "winner_loser_by_side.csv",
        "holding_time_summary.csv", "report.md",
    )
    hashes = {
        name: hashlib.sha256((output_dir / name).read_bytes()).hexdigest()
        for name in artifact_names
    }
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifact_hashes": hashes,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
