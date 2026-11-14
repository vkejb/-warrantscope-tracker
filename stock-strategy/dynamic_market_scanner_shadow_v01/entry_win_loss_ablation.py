"""Winner/loser audit and one-at-a-time causal entry gates, with no broker I/O."""
from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any

from fixed_take_profit_study_v01.study import discover_days
from paper_shadow_v01.runner import _load_market_context
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import SPEC

from .causal_auction import _verified_selected_quotes
from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY_RESULTS_DIR
from .signal_selection_rule_impact import (
    _fill, _fixed_30m, current_opportunity,
)
from .strict_replay import DEFAULT_RUNS_DIR, _simulate_exit, _write_csv


MODULE_DIR = Path(__file__).resolve().parent
SPEC_PATH = MODULE_DIR / "ENTRY_WIN_LOSS_ABLATION_SPEC.md"
DEFAULT_RESULTS_DIR = MODULE_DIR / "entry_win_loss_ablation_results"
GATES = (
    "BASE_RS1_180S", "MARKET_BULLISH", "OPENING_EXT_1PCT",
    "SPREAD_30BPS", "FLOW_120S_0_5", "RS_2PCT",
)
SCENARIOS = ("BASE", "STRESS")
PAPER_FEATURES = (
    "score", "volume_delta", "large_trade_delta", "vwap_gap",
    "book_imbalance", "spread_bps", "relative_strength_5m",
    "directional_opening_extension", "directional_vwap_extension",
    "entry_price", "benchmark_return_5m", "benchmark_vwap_gap",
)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def paper_gate(trade: dict[str, Any], gate: str) -> bool:
    if gate == "BASE_RS1_180S":
        return True
    if gate == "MARKET_BULLISH":
        return trade.get("market_regime") == "BULLISH"
    if gate == "FLOW_120S_0_5":
        raise ValueError("sealed paper has 60-second flow only")
    field, limit, comparison = {
        "OPENING_EXT_1PCT": ("directional_opening_extension", 0.01, "max"),
        "SPREAD_30BPS": ("spread_bps", 30.0, "max"),
        "RS_2PCT": ("relative_strength_5m", 0.02, "min"),
    }[gate]
    value = _number(trade.get(field))
    return value is not None and (value <= limit if comparison == "max" else value >= limit)


def paper_audit() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    days = discover_days()
    trades = [row["trade"] for row in days if row["trade"] is not None]
    winners = [row for row in trades if float(row["net_pnl"]) > 0]
    losers = [row for row in trades if float(row["net_pnl"]) < 0]
    features = []
    for field in PAPER_FEATURES:
        win = [_number(row.get(field)) for row in winners]
        loss = [_number(row.get(field)) for row in losers]
        win = [value for value in win if value is not None]
        loss = [value for value in loss if value is not None]
        features.append({
            "feature": field, "winner_count": len(win), "loser_count": len(loss),
            "winner_median": median(win) if win else None,
            "loser_median": median(loss) if loss else None,
            "winner_min": min(win) if win else None, "winner_max": max(win) if win else None,
            "loser_min": min(loss) if loss else None, "loser_max": max(loss) if loss else None,
            "ranges_overlap": bool(win and loss and max(min(win), min(loss)) <= min(max(win), max(loss))),
        })
    screen = []
    for gate in GATES:
        if gate == "FLOW_120S_0_5":
            screen.append({"gate": gate, "scope": "NOT_EVALUABLE_FROM_SEALED_PAPER_FIELDS",
                           "dates": len(days), "note": "120S_FLOW_MISSING_DO_NOT_SUBSTITUTE_60S"})
            continue
        kept = [row for row in trades if paper_gate(row, gate)]
        blocked = [row for row in trades if not paper_gate(row, gate)]
        screen.append({
            "gate": ("PAPER_UNCHANGED" if gate == GATES[0] else gate),
            "scope": "SEALED_FIRST_ENTRY_SKIP_TO_CASH_DIAGNOSTIC",
            "dates": len(days), "kept_trades": len(kept),
            "kept_winners": sum(float(row["net_pnl"]) > 0 for row in kept),
            "kept_losers": sum(float(row["net_pnl"]) < 0 for row in kept),
            "blocked_winners": sum(float(row["net_pnl"]) > 0 for row in blocked),
            "blocked_losers": sum(float(row["net_pnl"]) < 0 for row in blocked),
            "blocked_winner_pnl_twd": sum(float(row["net_pnl"]) for row in blocked if float(row["net_pnl"]) > 0),
            "blocked_loser_pnl_twd": sum(float(row["net_pnl"]) for row in blocked if float(row["net_pnl"]) < 0),
            "kept_net_pnl_twd": sum(float(row["net_pnl"]) for row in kept),
            "note": "NO_RESELECTION_AFTER_FIRST_ENTRY_REJECTED",
        })
    info = {
        "session_count": len(days), "trade_count": len(trades),
        "winners": len(winners), "losers": len(losers),
        "baseline_net_pnl_twd": sum(float(row["net_pnl"]) for row in trades),
        "source_manifest_hashes": {row["session_date"]: row["paper_manifest_hash"] for row in days},
    }
    return trades, features, screen, info


def _signed_volume(row: dict[str, Any]) -> float:
    flag = str(row.get("flag", ""))
    volume = float(row["volume"])
    if flag == "1":
        return volume
    if flag == "0":
        return -volume
    return volume if float(row["price"]) >= (float(row["bid"]) + float(row["ask"])) / 2 else -volume


def _pre_signal_flow(data: dict[str, Any], signal_time: datetime) -> tuple[float | None, int, int]:
    start = signal_time - timedelta(seconds=120)
    rows = [
        row for row in data["ticks"]
        if start <= row["time"] <= signal_time and row["received_at"] <= signal_time
    ]
    volume = sum(float(row["volume"]) for row in rows)
    proxy_count = sum(str(row.get("flag")) not in {"0", "1"} for row in rows)
    return (sum(_signed_volume(row) for row in rows) / volume if volume > 0 else None,
            len(rows), proxy_count)


def gate_passes(candidate: dict[str, Any], gate: str) -> tuple[bool, str]:
    if gate == "BASE_RS1_180S":
        return True, "PASSED"
    if gate == "MARKET_BULLISH":
        return candidate["market_regime"] == "BULLISH", "MARKET_NOT_BULLISH"
    if gate == "OPENING_EXT_1PCT":
        value = candidate.get("opening_extension")
        return value is not None and value <= 0.01, "OPENING_EXTENSION_ABOVE_1PCT_OR_UNKNOWN"
    if gate == "SPREAD_30BPS":
        return candidate["spread_bps"] <= 30.0, "SPREAD_ABOVE_30BPS"
    if gate == "FLOW_120S_0_5":
        value = candidate.get("pre_120s_volume_delta")
        return value is not None and value >= 0.5, "FLOW_BELOW_0_5_OR_UNKNOWN"
    if gate == "RS_2PCT":
        return candidate["relative_strength_5m"] >= 0.02, "RS_BELOW_2PCT"
    raise ValueError(f"unknown gate: {gate}")


def _account_metrics(rows: list[dict[str, Any]], dates: list[str]) -> dict[str, Any]:
    scored = [row for row in rows if row.get("status") == "SCORED"]
    unscored = [row for row in rows if row.get("status") != "SCORED"]
    values = [float(row["net_pnl_twd"]) for row in scored]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    by_day = {str(row["session_date"]): float(row["net_pnl_twd"]) for row in scored}
    equity = peak = 190_000.0
    max_dd = 0.0
    worst_day = 0.0
    for day in dates:
        value = by_day.get(day, 0.0)
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        worst_day = min(worst_day, value)
    return {
        "dates": len(dates), "selected_count": len(rows),
        "scored_count": len(scored), "unscored_count": len(unscored),
        "trade_days": len({row["session_date"] for row in scored}),
        "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "net_pnl_twd": sum(values) if not unscored else None,
        "scored_only_net_pnl_twd": sum(values),
        "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (math.inf if wins else None),
        "maximum_drawdown_twd": max_dd if not unscored else None,
        "worst_day_twd": worst_day if not unscored else None,
        "best_trade_twd": max(values) if values else None,
        "net_excluding_best_twd": sum(values) - max(values) if values and not unscored else None,
        "note": "FULL_ACCOUNT_RESULT" if not unscored else "PARTIAL_UNSCORABLE_EXPOSURE_NOT_ACCOUNT_PNL",
    }


def replay_early_candidate_gates() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    source_path = EARLY_RESULTS_DIR / "signals.csv"
    source = _rows(source_path)
    cache_dir = EARLY_RESULTS_DIR / "replay_cache"
    dates = sorted({row["session_date"] for row in source})
    ledger: list[dict[str, Any]] = []
    account: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}
    for day in dates:
        cached = json.loads((cache_dir / f"{day}.json").read_text(encoding="utf-8"))["replay"]
        run_dir = DEFAULT_RUNS_DIR / cached["source_run_id"]
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if manifest["manifest_hash"] != cached["source_manifest_hash"]:
            raise RuntimeError(f"archive identity mismatch: {day}")
        source_hashes[day] = manifest["manifest_hash"]
        rows = [dict(row) for row in source if row["session_date"] == day]
        for row in rows:
            row["signal_time"] = datetime.fromisoformat(row["signal_time"])
            row["entry_price"] = float(row["entry_price"])
            row["relative_strength_5m"] = float(row["relative_strength_5m"])
        candidates = _verified_selected_quotes(run_dir, {row["stock_id"] for row in rows})
        market = _load_market_context(run_dir, manifest)
        coverage = {"session_date": day, "ended_at_taipei": manifest["ended_at"]}
        eligible: list[dict[str, Any]] = []
        for row in rows:
            data = candidates[row["stock_id"]]
            opportunity = current_opportunity(row, data)
            features = {}
            if opportunity["passed"]:
                flow, count, proxy_count = _pre_signal_flow(data, row["signal_time"])
                known = [tick for tick in data["ticks"] if tick["time"] <= opportunity["effective_time"]]
                opening = float(known[0]["price"]) if known else None
                features = {
                    "pre_120s_volume_delta": flow,
                    "pre_120s_tick_count": count,
                    "pre_120s_midpoint_proxy_count": proxy_count,
                    "opening_extension": (
                        float(opportunity["entry_price"]) / opening - 1
                        if opening else None
                    ),
                    "spread_bps": float(opportunity["spread_bps"]),
                }
                eligible.append({**row, **opportunity, **features})
            ledger.append({
                "session_date": day, "stock_id": row["stock_id"],
                "signal_time": row["signal_time"].isoformat(),
                "market_regime": row["market_regime"],
                "relative_strength_5m": row["relative_strength_5m"],
                "base_passed": bool(opportunity["passed"]),
                "base_reason": opportunity["reason"],
                **features,
            })
        for gate in GATES:
            passed = [item for item in eligible if gate_passes(item, gate)[0]]
            passed.sort(key=lambda item: (
                item["effective_time"], -item["relative_strength_5m"], item["stock_id"],
            ))
            selected = passed[0] if passed else None
            for item in eligible:
                accepted, reason = gate_passes(item, gate)
                fill, fill_reason = _fill(item, candidates[item["stock_id"]], "BASE")
                mark = _fixed_30m(item, fill, candidates[item["stock_id"]]) if fill else None
                ledger.append({
                    "session_date": day, "stock_id": item["stock_id"],
                    "signal_time": item["signal_time"].isoformat(),
                    "gate": gate, "gate_passed": accepted,
                    "gate_reason": "PASSED" if accepted else reason,
                    "selected_for_account": selected is item,
                    "fixed_30m_net_pnl_twd": mark,
                    "fixed_30m_status": "SCORED" if mark is not None else fill_reason,
                    "market_regime": item["market_regime"],
                    "relative_strength_5m": item["relative_strength_5m"],
                    **{key: item.get(key) for key in (
                        "pre_120s_volume_delta", "pre_120s_tick_count",
                        "pre_120s_midpoint_proxy_count", "opening_extension", "spread_bps",
                    )},
                })
            for scenario in SCENARIOS:
                if selected is None:
                    daily.append({"session_date": day, "gate": gate, "scenario": scenario,
                                  "status": "NO_ENTRY", "net_pnl_twd": 0})
                    continue
                data = candidates[selected["stock_id"]]
                fill, reason = _fill(selected, data, scenario)
                if fill is None:
                    result = {"status": "UNFILLED", "reason": reason, "net_pnl_twd": None}
                else:
                    payload = {
                        **selected, "confirmation_time": fill["entry_time"],
                        "delayed_entry_price": fill["entry_price"],
                        "delayed_entry_quantity": fill["quantity"],
                    }
                    trade = _simulate_exit(
                        payload, {selected["stock_id"]: data}, market, coverage,
                    )
                    result = {**trade, "net_pnl_twd": trade.get("net_pnl")}
                record = {
                    **result, "session_date": day, "gate": gate, "scenario": scenario,
                    "stock_id": selected["stock_id"],
                    "signal_time": selected["signal_time"].isoformat(),
                    "effective_time": selected["effective_time"].isoformat(),
                    "candidate_rank": 1,
                    "eligible_candidate_count": len(passed),
                    "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
                }
                account.append(record)
                daily.append({
                    "session_date": day, "gate": gate, "scenario": scenario,
                    "status": result["status"], "stock_id": selected["stock_id"],
                    "net_pnl_twd": result.get("net_pnl_twd"),
                })
    experiments = []
    for gate in GATES:
        for scenario in SCENARIOS:
            rows = [row for row in account if row["gate"] == gate and row["scenario"] == scenario]
            metric = _account_metrics(rows, dates)
            gate_ledger = [row for row in ledger if row.get("gate") == gate]
            blocked = [row for row in gate_ledger if row.get("gate_passed") is False]
            good = [row for row in blocked if _number(row.get("fixed_30m_net_pnl_twd")) is not None and float(row["fixed_30m_net_pnl_twd"]) > 0]
            bad = [row for row in blocked if _number(row.get("fixed_30m_net_pnl_twd")) is not None and float(row["fixed_30m_net_pnl_twd"]) < 0]
            flat = [row for row in blocked if _number(row.get("fixed_30m_net_pnl_twd")) == 0]
            experiments.append({
                "gate": gate, "scenario": scenario,
                "base_eligible_signal_count": len(gate_ledger),
                "gate_pass_signal_count": sum(row["gate_passed"] is True for row in gate_ledger),
                "blocked_positive_30m_count": len(good),
                "blocked_negative_30m_count": len(bad),
                "blocked_flat_30m_count": len(flat),
                "blocked_unscorable_30m_count": len(blocked) - len(good) - len(bad) - len(flat),
                **metric,
            })
    return ledger, account, daily, experiments, source_hashes


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float) and math.isinf(value):
        return "INF"
    return f"{value:.{digits}f}" if isinstance(value, (float, int)) else str(value)


def _daily_changes(account: list[dict[str, Any]], dates: list[str]) -> list[dict[str, Any]]:
    by_key = {(row["session_date"], row["gate"], row["scenario"]): row for row in account}
    output = []
    for scenario in SCENARIOS:
        for day in dates:
            original = by_key.get((day, GATES[0], scenario))
            for gate in GATES[1:]:
                updated = by_key.get((day, gate, scenario))
                before = original.get("stock_id") if original else None
                after = updated.get("stock_id") if updated else None
                classification = (
                    "UNCHANGED" if before == after else
                    "SKIPPED_TO_CASH" if after is None else
                    "NEW_ENTRY" if before is None else "RESELECTED"
                )
                prior_pnl = original.get("net_pnl_twd") if original else 0.0
                next_pnl = updated.get("net_pnl_twd") if updated else 0.0
                output.append({
                    "session_date": day, "scenario": scenario, "gate": gate,
                    "change": classification, "baseline_stock_id": before,
                    "gate_stock_id": after,
                    "baseline_status": original.get("status") if original else "NO_ENTRY",
                    "gate_status": updated.get("status") if updated else "NO_ENTRY",
                    "baseline_net_pnl_twd": prior_pnl,
                    "gate_net_pnl_twd": next_pnl,
                    "delta_twd": (
                        float(next_pnl) - float(prior_pnl)
                        if prior_pnl is not None and next_pnl is not None else None
                    ),
                })
    return output


def _add_daily_equity(daily: list[dict[str, Any]], dates: list[str]) -> None:
    by_key = {(row["session_date"], row["gate"], row["scenario"]): row for row in daily}
    for gate in GATES:
        for scenario in SCENARIOS:
            equity = peak = 190_000.0
            unknown = False
            for day in dates:
                row = by_key[(day, gate, scenario)]
                pnl = row.get("net_pnl_twd")
                if pnl is None:
                    unknown = True
                if unknown:
                    row["equity_twd"] = None
                    row["drawdown_twd"] = None
                    continue
                equity += float(pnl)
                peak = max(peak, equity)
                row["equity_twd"] = equity
                row["drawdown_twd"] = peak - equity


def _post_entry_summary(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for label, selected in (
        ("WIN", [row for row in trades if float(row["net_pnl"]) > 0]),
        ("LOSS", [row for row in trades if float(row["net_pnl"]) < 0]),
    ):
        for field in (
            "mfe_net_pnl_at_exit", "mae_net_pnl_at_exit",
            "full_path_mfe_net_pnl", "full_path_mae_net_pnl",
            "holding_seconds",
        ):
            values = [_number(row.get(field)) for row in selected]
            values = [value for value in values if value is not None]
            output.append({
                "outcome": label, "feature": field, "count": len(values),
                "median": median(values) if values else None,
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
                "timing": "POST_ENTRY_DIAGNOSTIC_NOT_ENTRY_FILTER",
            })
    return output


def _report(info: dict[str, Any], features: list[dict[str, Any]], screen: list[dict[str, Any]], experiments: list[dict[str, Any]], changes: list[dict[str, Any]], post_entry: list[dict[str, Any]], paper_trades: list[dict[str, Any]]) -> str:
    lines = [
        "# 擴大池獲利／虧損訊號差異與逐項回測", "",
        f"封存正式紙上選中：{info['session_count']}日、{info['trade_count']}筆，{info['winners']}勝／{info['losers']}敗，成本後 {_fmt(info['baseline_net_pnl_twd'])} 元。",
        "獲利與虧損是依原 MFE 出場的已實現損益標記；此樣本很小且全部已知。", "",
        "## 進場當下差異", "",
        "| 特徵 | 獲利中位數 | 虧損中位數 | 範圍重疊 |",
        "|---|---:|---:|---|",
    ]
    for row in features:
        lines.append(f"| {row['feature']} | {_fmt(row['winner_median'],4)} | {_fmt(row['loser_median'],4)} | {'是' if row['ranges_overlap'] else '否'} |")
    lines += ["", "`market_regime` 及訊號時間另見逐筆表；4筆獲利均為 BULLISH，但12/14筆虧損也為 BULLISH。", ""]
    lines += [
        "### 進場後路徑（只作診斷）", "",
        "| 結果 | MFE中位數 | MAE中位數 | 完整路徑MFE中位數 | 持有秒數中位數 | 出場原因 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for label in ("WIN", "LOSS"):
        selected = [row for row in paper_trades if (float(row["net_pnl"]) > 0) == (label == "WIN")]
        metric = {row["feature"]: row["median"] for row in post_entry if row["outcome"] == label}
        reasons = Counter(str(row["exit_reason"]) for row in selected)
        lines.append(
            f"| {label} | {_fmt(metric['mfe_net_pnl_at_exit'])} | {_fmt(metric['mae_net_pnl_at_exit'])} | "
            f"{_fmt(metric['full_path_mfe_net_pnl'])} | {_fmt(metric['holding_seconds'])} | "
            f"{', '.join(f'{key}:{value}' for key, value in sorted(reasons.items()))} |"
        )
    lines += ["", "這些數值在進場之後才知道，不可回填進場篩選。", ""]
    lines += [
        "## 逐項篩選：原正式紙上第一筆，拒絕後空手（只作診斷）", "",
        "| 條件 | 保留 | 擋掉獲利／虧損 | 保留損益 |",
        "|---|---:|---:|---:|",
    ]
    for row in screen:
        if "kept_trades" not in row:
            lines.append(f"| {row['gate']} | NA | NA | NA |")
        else:
            lines.append(f"| {row['gate']} | {row['kept_trades']} | {row['blocked_winners']}/{row['blocked_losers']} | {_fmt(row['kept_net_pnl_twd'])} |")
    lines += ["", "這張表不能當完整帳戶成效：第一筆被擋後，當日可能有其他訊號。", ""]
    lines += [
        "## 逐項篩選：早期就緒研究池，重新選股的帳戶重播", "",
        "18個已知日期、相同19萬元／每日一筆／原出場與成本。此池的基準是 `CURRENT_RS1_180S`，與上表正式紙上進場不同。",
        "只有所有出場可評分時，`net_pnl_twd` 才是完整帳戶損益；否則留 NA，另列已評分部分。", "",
        "| 情境 | 條件 | 通過訊號 | 帳戶成交／不可評 | 淨損益 | PF | 最大回撤 | 擋掉30分正／負訊號 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in experiments:
        lines.append(
            f"| {row['scenario']} | {row['gate']} | {row['gate_pass_signal_count']} | "
            f"{row['scored_count']}/{row['unscored_count']} | {_fmt(row['net_pnl_twd'])} | "
            f"{_fmt(row['profit_factor'],3)} | {_fmt(row['maximum_drawdown_twd'])} | "
            f"{row['blocked_positive_30m_count']}/{row['blocked_negative_30m_count']} |"
        )
    lines += ["", "### BASE 逐日改選或空手的影響", "",
              "| 條件 | 改選日 | 空手日 | BASE 帳戶相對基準增量 |",
              "|---|---:|---:|---:|"]
    for gate in GATES[1:]:
        subset = [row for row in changes if row["gate"] == gate and row["scenario"] == "BASE"]
        metric = next(row for row in experiments if row["gate"] == gate and row["scenario"] == "BASE")
        lines.append(
            f"| {gate} | {sum(row['change'] == 'RESELECTED' for row in subset)} | "
            f"{sum(row['change'] == 'SKIPPED_TO_CASH' for row in subset)} | "
            f"{_fmt(metric['incremental_vs_base_twd'])} |"
        )
    lines += [
        "", "所有候選的30分鐘正負結果是重疊訊號診斷，不可加總成帳戶損益；未被選中也不是未來一定可成交。",
        "同日與同股票訊號高度相關，這些逐項實驗沒有真正新日期驗證。",
        "研究沒有券商委託；成交價與股數沿用既有代理模型，方向欄位與市場衝擊仍為條件假設。", "",
    ]
    return "\n".join(lines)


def run(results_dir: Path = DEFAULT_RESULTS_DIR) -> dict[str, Any]:
    if results_dir.exists() and any(results_dir.iterdir()):
        raise RuntimeError(f"refusing to overwrite research results: {results_dir}")
    paper_trades, features, screen, paper_info = paper_audit()
    post_entry = _post_entry_summary(paper_trades)
    ledger, account, daily, experiments, source_hashes = replay_early_candidate_gates()
    baseline = next(row for row in experiments if row["gate"] == GATES[0] and row["scenario"] == "BASE")
    if baseline["net_pnl_twd"] != 5899.0 or baseline["scored_count"] != 9:
        raise RuntimeError(f"prior frozen baseline parity failed: {baseline}")
    for row in experiments:
        row["incremental_vs_base_twd"] = (
            row["net_pnl_twd"] - baseline["net_pnl_twd"]
            if row["net_pnl_twd"] is not None else None
        )
    dates = sorted(source_hashes)
    changes = _daily_changes(account, dates)
    _add_daily_equity(daily, dates)
    results_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(results_dir / "paper_selected_trades.csv", paper_trades)
    _write_csv(results_dir / "paper_feature_comparison.csv", features)
    _write_csv(results_dir / "paper_post_entry_comparison.csv", post_entry)
    _write_csv(results_dir / "paper_skip_to_cash.csv", screen)
    _write_csv(results_dir / "candidate_gate_ledger.csv", ledger)
    _write_csv(results_dir / "account_trades.csv", account)
    _write_csv(results_dir / "daily_results.csv", daily)
    _write_csv(results_dir / "daily_selection_changes.csv", changes)
    _write_csv(results_dir / "experiments.csv", experiments)
    (results_dir / "REPORT.md").write_text(
        _report(paper_info, features, screen, experiments, changes, post_entry, paper_trades),
        encoding="utf-8",
    )
    summary = {
        "analysis_id": "ENTRY_WIN_LOSS_ABLATION_V0_1",
        "spec_hash": sha256_file(SPEC_PATH),
        "paper": paper_info, "early_source_hashes": source_hashes,
        "early_signal_count": len([row for row in ledger if "gate" not in row]),
        "early_base_eligible_count": baseline["base_eligible_signal_count"],
        "experiments": experiments,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "interpretation": "KNOWN_ARCHIVE_EXPLORATORY_NO_LIVE_APPROVAL",
    }
    (results_dir / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    artifacts = {path.name: sha256_file(path) for path in results_dir.iterdir() if path.is_file()}
    manifest = {
        "analysis_id": summary["analysis_id"], "spec_hash": summary["spec_hash"],
        "code_hash": sha256_file(Path(__file__)), "artifacts": artifacts,
        "reproduce": "PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.entry_win_loss_ablation",
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (results_dir / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return summary


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    args = parser.parse_args()
    result = run(args.output_dir)
    print(json.dumps({"analysis_id": result["analysis_id"],
                      "output_dir": str(args.output_dir),
                      "experiments": result["experiments"]}, ensure_ascii=False))
