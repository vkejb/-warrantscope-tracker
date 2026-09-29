"""Controlled signal-quality diagnostics; never changes strategy behavior."""
from __future__ import annotations

import csv
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping

from trade_path_diagnostics_v01.analysis import build_report as build_path_report
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _decision_times,
    _entry_tick,
    _tick_size,
    load_session,
    signal_at,
)


ANALYSIS_ID = "SIGNAL_QUALITY_DIAGNOSTICS_V0_1"
FEATURES = ("score", "volume_strength", "large_trade_strength")
BUCKETS = (
    ("LT_0_55", -math.inf, 0.55),
    ("0_55_TO_0_65", 0.55, 0.65),
    ("0_65_TO_0_75", 0.65, 0.75),
    ("GE_0_75", 0.75, math.inf),
)
THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80)


def _safe_mean(values: Iterable[float | None]) -> float | None:
    kept = [float(value) for value in values if value is not None]
    return mean(kept) if kept else None


def _safe_median(values: Iterable[float | None]) -> float | None:
    kept = [float(value) for value in values if value is not None]
    return median(kept) if kept else None


def _feature_rows(
    session_runs: Mapping[str, list[Path]], capital: int,
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    coverages = []
    for _requested_date, run_dirs in sorted(session_runs.items()):
        stocks, coverage = load_session(run_dirs)
        session_date = str(coverage["session_date"])
        coverages.append(coverage)
        attempted: set[str] = set()
        for decision in _decision_times(
            session_date, str(SPEC["entry_start"]), str(SPEC["last_entry_time"]),
        ):
            for stock_id, data in stocks.items():
                if stock_id in attempted:
                    continue
                signal = signal_at(stock_id, data, decision)
                if signal is None:
                    continue
                entry_tick = _entry_tick(data, decision)
                if entry_tick is None:
                    continue
                entry_price = (
                    entry_tick["ask"] + _tick_size(entry_tick["ask"])
                    if signal.side == "LONG"
                    else max(
                        _tick_size(entry_tick["bid"]),
                        entry_tick["bid"] - _tick_size(entry_tick["bid"]),
                    )
                )
                if entry_price * 1000 > capital:
                    continue
                attempted.add(stock_id)
                causal = [
                    row for row in data["ticks"]
                    if row["time"] <= decision
                ]
                current_price = float(causal[-1]["price"])

                def directional_return(seconds: int) -> float | None:
                    prior = [
                        row for row in causal
                        if row["time"] <= decision - timedelta(seconds=seconds)
                    ]
                    if not prior or float(prior[-1]["price"]) <= 0:
                        return None
                    raw = current_price / float(prior[-1]["price"]) - 1
                    return raw if signal.side == "LONG" else -raw

                breakout_start = decision - timedelta(
                    seconds=int(SPEC["breakout_lookback_seconds"])
                )
                breakout_end = decision - timedelta(
                    seconds=int(SPEC["breakout_excludes_latest_seconds"])
                )
                breakout_rows = [
                    row for row in causal
                    if breakout_start <= row["time"] <= breakout_end
                ]
                if signal.side == "LONG":
                    boundary = max(float(row["price"]) for row in breakout_rows)
                    breakout_overshoot = current_price / boundary - 1
                    opening_extension = current_price / float(causal[0]["price"]) - 1
                    directional_vwap_extension = signal.vwap_gap
                else:
                    boundary = min(float(row["price"]) for row in breakout_rows)
                    breakout_overshoot = boundary / current_price - 1
                    opening_extension = float(causal[0]["price"]) / current_price - 1
                    directional_vwap_extension = -signal.vwap_gap
                rows[(session_date, stock_id)] = {
                    "signal_time": signal.decision_time.isoformat(),
                    "score": signal.score,
                    "volume_delta": signal.volume_delta,
                    "volume_strength": abs(signal.volume_delta),
                    "large_trade_delta": signal.large_trade_delta,
                    "large_trade_strength": abs(signal.large_trade_delta),
                    "vwap_gap": signal.vwap_gap,
                    "directional_vwap_extension": directional_vwap_extension,
                    "directional_return_60s": directional_return(60),
                    "directional_return_300s": directional_return(300),
                    "directional_opening_extension": opening_extension,
                    "breakout_boundary_price": boundary,
                    "breakout_overshoot": breakout_overshoot,
                    "book_imbalance": signal.book_imbalance,
                    "spread_bps": signal.spread_bps,
                }
    return rows, coverages


def _outcome_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for outcome in ("WINNER", "LOSER", "UNSCORABLE"):
        selected = [row for row in rows if row["outcome"] == outcome]
        output.append({
            "outcome": outcome,
            "trades": len(selected),
            "average_score": _safe_mean(row["score"] for row in selected),
            "median_score": _safe_median(row["score"] for row in selected),
            "average_volume_strength": _safe_mean(
                row["volume_strength"] for row in selected
            ),
            "median_volume_strength": _safe_median(
                row["volume_strength"] for row in selected
            ),
            "average_large_trade_strength": _safe_mean(
                row["large_trade_strength"] for row in selected
            ),
            "median_large_trade_strength": _safe_median(
                row["large_trade_strength"] for row in selected
            ),
            "average_realized_net_pnl": _safe_mean(
                row["realized_net_pnl"] for row in selected
            ),
            "average_held_mfe_net_pnl": _safe_mean(
                row["held_mfe_net_pnl"] for row in selected
            ),
        })
    return output


def _side_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for side in ("LONG", "SHORT"):
        for outcome in ("WINNER", "LOSER"):
            selected = [
                row for row in rows
                if row["side"] == side and row["outcome"] == outcome
            ]
            output.append({
                "side": side,
                "outcome": outcome,
                "trades": len(selected),
                "average_score": _safe_mean(row["score"] for row in selected),
                "average_volume_strength": _safe_mean(
                    row["volume_strength"] for row in selected
                ),
                "average_large_trade_strength": _safe_mean(
                    row["large_trade_strength"] for row in selected
                ),
                "average_realized_net_pnl": _safe_mean(
                    row["realized_net_pnl"] for row in selected
                ),
            })
    return output


def _session_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    dates = sorted({str(row["session_date"]) for row in rows})
    for session_date in dates:
        selected = [
            row for row in rows
            if str(row["session_date"]) == session_date
            and row["outcome"] in {"WINNER", "LOSER"}
        ]
        winners = [row for row in selected if row["outcome"] == "WINNER"]
        losers = [row for row in selected if row["outcome"] == "LOSER"]
        output.append({
            "session_date": session_date,
            "trades": len(selected),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": len(winners) / len(selected) if selected else None,
            "winner_average_score": _safe_mean(row["score"] for row in winners),
            "loser_average_score": _safe_mean(row["score"] for row in losers),
            "winner_average_volume_strength": _safe_mean(
                row["volume_strength"] for row in winners
            ),
            "loser_average_volume_strength": _safe_mean(
                row["volume_strength"] for row in losers
            ),
            "average_realized_net_pnl": _safe_mean(
                row["realized_net_pnl"] for row in selected
            ),
            "total_realized_net_pnl": sum(
                float(row["realized_net_pnl"]) for row in selected
            ),
        })
    return output


def _score_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [row for row in rows if row["outcome"] in {"WINNER", "LOSER"}]
    output = []
    for label, lower, upper in BUCKETS:
        selected = [row for row in scored if lower <= float(row["score"]) < upper]
        winners = sum(row["outcome"] == "WINNER" for row in selected)
        output.append({
            "score_bucket": label,
            "trades": len(selected),
            "winners": winners,
            "losers": len(selected) - winners,
            "win_rate": winners / len(selected) if selected else None,
            "average_score": _safe_mean(row["score"] for row in selected),
            "average_realized_net_pnl": _safe_mean(
                row["realized_net_pnl"] for row in selected
            ),
            "total_realized_net_pnl": sum(
                float(row["realized_net_pnl"]) for row in selected
            ),
        })
    return output


def _threshold_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [row for row in rows if row["outcome"] in {"WINNER", "LOSER"}]
    output = []
    for threshold in THRESHOLDS:
        for group, selected in (
            ("AT_OR_ABOVE", [row for row in scored if row["score"] >= threshold]),
            ("BELOW", [row for row in scored if row["score"] < threshold]),
        ):
            winners = sum(row["outcome"] == "WINNER" for row in selected)
            output.append({
                "threshold": threshold,
                "group": group,
                "trades": len(selected),
                "winners": winners,
                "win_rate": winners / len(selected) if selected else None,
                "average_realized_net_pnl": _safe_mean(
                    row["realized_net_pnl"] for row in selected
                ),
                "total_realized_net_pnl": sum(
                    float(row["realized_net_pnl"]) for row in selected
                ),
            })
    return output


def _ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2
        for original, _value in ordered[index:end]:
            ranks[original] = rank
        index = end
    return ranks


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_ss = sum((x - left_mean) ** 2 for x in left)
    right_ss = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_ss * right_ss)
    return numerator / denominator if denominator else None


def _spearman(left: list[float], right: list[float]) -> float | None:
    return _pearson(_ranks(left), _ranks(right))


def _correlations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = [row for row in rows if row["outcome"] in {"WINNER", "LOSER"}]
    pnl = [float(row["realized_net_pnl"]) for row in scored]
    mfe = [float(row["held_mfe_net_pnl"]) for row in scored]
    win = [1.0 if row["outcome"] == "WINNER" else 0.0 for row in scored]
    output = []
    for feature in FEATURES:
        values = [float(row[feature]) for row in scored]
        pnl_corr = _spearman(values, pnl)
        mfe_corr = _spearman(values, mfe)
        win_corr = _spearman(values, win)
        loo_pnl = []
        loo_win = []
        for omitted in range(len(scored)):
            kept_feature = values[:omitted] + values[omitted + 1:]
            kept_pnl = pnl[:omitted] + pnl[omitted + 1:]
            kept_win = win[:omitted] + win[omitted + 1:]
            current_pnl = _spearman(kept_feature, kept_pnl)
            current_win = _spearman(kept_feature, kept_win)
            if current_pnl is not None:
                loo_pnl.append(current_pnl)
            if current_win is not None:
                loo_win.append(current_win)
        output.append({
            "feature": feature,
            "trades": len(scored),
            "spearman_vs_realized_pnl": pnl_corr,
            "spearman_vs_held_mfe": mfe_corr,
            "spearman_vs_win_indicator": win_corr,
            "leave_one_out_pnl_min": min(loo_pnl) if loo_pnl else None,
            "leave_one_out_pnl_max": max(loo_pnl) if loo_pnl else None,
            "leave_one_out_win_min": min(loo_win) if loo_win else None,
            "leave_one_out_win_max": max(loo_win) if loo_win else None,
            "positive_relationship_supported": bool(
                pnl_corr is not None
                and pnl_corr > 0
                and loo_pnl
                and min(loo_pnl) > 0
            ),
        })
    return output


def build_report(
    session_runs: Mapping[str, list[Path]], capital: int = 190_000,
) -> dict[str, Any]:
    path_report = build_path_report(session_runs, capital)
    features, feature_coverages = _feature_rows(session_runs, capital)
    trade_rows = []
    for row in path_report["trade_summary"]:
        key = (str(row["session_date"]), str(row["symbol"]))
        feature = features.get(key)
        if feature is None:
            raise RuntimeError(f"missing signal features for {key[0]} {key[1]}")
        trade_rows.append({**row, **feature})
    outcome_summary = _outcome_summary(trade_rows)
    correlations = _correlations(trade_rows)
    return {
        "analysis_id": ANALYSIS_ID,
        "interpretation": (
            "RETROSPECTIVE_SIGNAL_QUALITY_DIAGNOSTIC_ONLY; SCORE IS AN INPUT "
            "COMPOSITE, NOT A CALIBRATED WIN PROBABILITY"
        ),
        "trade_universe": path_report["trade_universe"],
        "outcome_policy": path_report["outcome_policy"],
        "capital_twd": capital,
        "feature_definitions": {
            "score": (
                "0.35*abs(volume_delta)+0.25*abs(large_trade_delta)+"
                "0.20*capped_vwap_gap_strength+0.20*abs(book_imbalance)"
            ),
            "volume_strength": "abs(volume_delta)",
            "large_trade_strength": "abs(large_trade_delta)",
        },
        "coverage": feature_coverages,
        "per_trade": trade_rows,
        "outcome_summary": outcome_summary,
        "side_summary": _side_summary(trade_rows),
        "session_summary": _session_summary(trade_rows),
        "score_buckets": _score_buckets(trade_rows),
        "threshold_analysis": _threshold_rows(trade_rows),
        "feature_correlations": correlations,
        "counts": path_report["counts"],
        "sample_warning": (
            "Only 17 trades are scorable. Results are descriptive and must not "
            "be used to tune or enable a live threshold."
        ),
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


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{float(value):,.{digits}f}"


def _report_markdown(report: dict[str, Any]) -> str:
    summary = {row["outcome"]: row for row in report["outcome_summary"]}
    winner, loser = summary["WINNER"], summary["LOSER"]
    lines = [
        "# Signal quality diagnostic",
        "",
        "This is a retrospective, read-only diagnostic. It does not change entry, exit, sizing, or live behavior.",
        "",
        f"- Trades: {report['counts']['trades']} ({report['counts']['scored']} scorable)",
        f"- Winners / losers: {report['counts']['winners']} / {report['counts']['losers']}",
        "- Independent signals may overlap and are not one executable portfolio.",
        "",
        "## Winner versus loser",
        "",
        "| Outcome | Trades | Avg score | Avg volume strength | Avg large-trade strength | Avg net PnL | Avg held MFE |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in (winner, loser):
        lines.append(
            f"| {row['outcome']} | {row['trades']} | {_fmt(row['average_score'])} | "
            f"{_fmt(row['average_volume_strength'])} | {_fmt(row['average_large_trade_strength'])} | "
            f"{_fmt(row['average_realized_net_pnl'], 0)} | {_fmt(row['average_held_mfe_net_pnl'], 0)} |"
        )
    lines.extend([
        "",
        "## Score buckets",
        "",
        "| Score bucket | Trades | Winners | Win rate | Avg net PnL | Total net PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in report["score_buckets"]:
        rate = "N/A" if row["win_rate"] is None else f"{row['win_rate'] * 100:.1f}%"
        lines.append(
            f"| {row['score_bucket']} | {row['trades']} | {row['winners']} | {rate} | "
            f"{_fmt(row['average_realized_net_pnl'], 0)} | {_fmt(row['total_realized_net_pnl'], 0)} |"
        )
    lines.extend([
        "",
        "## Session split",
        "",
        "| Session | Trades | Winners | Winner avg score | Loser avg score | Total net PnL |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in report["session_summary"]:
        lines.append(
            f"| {row['session_date']} | {row['trades']} | {row['winners']} | "
            f"{_fmt(row['winner_average_score'])} | {_fmt(row['loser_average_score'])} | "
            f"{_fmt(row['total_realized_net_pnl'], 0)} |"
        )
    lines.extend([
        "",
        "## Correlation diagnostic",
        "",
        "| Feature | Spearman vs net PnL | Spearman vs held MFE | Spearman vs win | LOO PnL range | Stable positive? |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in report["feature_correlations"]:
        lines.append(
            f"| {row['feature']} | {_fmt(row['spearman_vs_realized_pnl'])} | "
            f"{_fmt(row['spearman_vs_held_mfe'])} | "
            f"{_fmt(row['spearman_vs_win_indicator'])} | "
            f"{_fmt(row['leave_one_out_pnl_min'])} to {_fmt(row['leave_one_out_pnl_max'])} | "
            f"{row['positive_relationship_supported']} |"
        )
    winner_score = float(winner["average_score"])
    loser_score = float(loser["average_score"])
    conclusion = (
        "Losers have the higher average composite score in this sample. The score measures "
        "the intensity of recent flow and breakout conditions, but it is not calibrated as "
        "a probability of profit. Strong readings can also occur during late-stage acceleration."
        if loser_score > winner_score else
        "Winners have the higher average score in this sample, but the sample remains too small "
        "to justify a live threshold."
    )
    lines.extend([
        "",
        "## Conclusion",
        "",
        conclusion,
        "",
        "All three archived sessions are labelled PARTIAL_SESSION by their source manifests; 2026-09-23 also records callback errors. These limitations make the results useful for diagnosis, not production validation.",
        "",
        "Do not add a minimum-score production filter from this sample. Continue collecting full-session data and test whether score interacts with extension, time of day, post-entry flow decay, and market regime.",
        "",
    ])
    return "\n".join(lines)


def write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "summary.json": json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        "report.md": _report_markdown(report),
    }
    for name, content in payloads.items():
        (output_dir / name).write_text(content, encoding="utf-8")
    csv_rows = {
        "per_trade_signal_quality.csv": report["per_trade"],
        "outcome_summary.csv": report["outcome_summary"],
        "side_summary.csv": report["side_summary"],
        "session_summary.csv": report["session_summary"],
        "score_buckets.csv": report["score_buckets"],
        "threshold_analysis.csv": report["threshold_analysis"],
        "feature_correlations.csv": report["feature_correlations"],
    }
    for name, rows in csv_rows.items():
        _write_csv(output_dir / name, rows)
    artifacts = {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "run_manifest.json"
    }
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "artifact_hashes": artifacts,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "strategy_changed": False,
        "live_behavior_changed": False,
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
