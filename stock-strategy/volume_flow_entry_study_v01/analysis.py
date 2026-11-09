"""Bounded causal volume-flow replay for the current production LONG strategy.

The module is deliberately broker-free.  It reads immutable quote archives and
reuses the current signal/exit engine, but it cannot submit or reconcile orders.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping

from entry_quality_edge_study_v01.analysis import (
    SessionData,
    _ScenarioEngine,
    _flow_metrics,
    discover_sessions,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _decision_times,
    _projected_net_pnl,
    _tick_size,
)
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    _feed_until,
    _parse_stamp,
    _record_book,
    _record_tick,
)
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LIVE_EXIT_POLICY,
    LONG_MARKET_REGIME_POLICY,
    LiveSignal,
    ManagedPosition,
)


MODULE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = MODULE_DIR / "config.json"
SPEC_PATH = MODULE_DIR / "EXPERIMENT_SPEC.md"
DEFAULT_RESULTS_DIR = MODULE_DIR / "results"
VARIANTS = (
    "V0_CURRENT_PRODUCTION",
    "V1_SUSTAINED_DISTRIBUTED_BUYING",
    "V2_RECENT_VOLUME_ACCELERATION",
    "V3_SUSTAINED_PLUS_ACCELERATION",
)
SCENARIOS = ("BASE", "STRESS")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(dict(config))).hexdigest()


def _window_complete(
    metrics: Mapping[str, Any], *, minimum_ticks: int,
) -> bool:
    return (
        int(metrics.get("tick_count") or 0) >= minimum_ticks
        and metrics.get("volume_delta") is not None
        and metrics.get("buyer_tick_ratio") is not None
        and metrics.get("largest_buy_share") is not None
    )


def evaluate_volume_gate(
    data: Mapping[str, Any],
    decision: datetime,
    variant: str,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """Evaluate a frozen entry gate using only data available by decision."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    if variant == "V0_CURRENT_PRODUCTION":
        return {
            "evaluable": True,
            "passed": True,
            "reason": "BASELINE_NO_ADDITIONAL_VOLUME_GATE",
        }

    rules = config["variants"]
    sustained_cfg = rules["V1_SUSTAINED_DISTRIBUTED_BUYING"]
    acceleration_cfg = rules["V2_RECENT_VOLUME_ACCELERATION"]
    sustained = _flow_metrics(
        data,
        start=decision - timedelta(seconds=int(sustained_cfg["pre_window_seconds"])),
        end=decision,
    )
    concentration = _flow_metrics(
        data,
        start=decision - timedelta(seconds=int(sustained_cfg["concentration_window_seconds"])),
        end=decision,
    )
    recent_seconds = int(acceleration_cfg["window_seconds"])
    recent = _flow_metrics(
        data, start=decision - timedelta(seconds=recent_seconds), end=decision,
    )
    previous = _flow_metrics(
        data,
        start=decision - timedelta(seconds=recent_seconds * 2),
        end=decision - timedelta(seconds=recent_seconds),
    )
    ratio = (
        float(recent["volume"]) / float(previous["volume"])
        if float(previous.get("volume") or 0) > 0 else None
    )
    features = {
        "pre_120s_tick_count": sustained.get("tick_count"),
        "pre_120s_volume_delta": sustained.get("volume_delta"),
        "pre_120s_buyer_tick_ratio": sustained.get("buyer_tick_ratio"),
        "pre_30s_largest_buy_share": concentration.get("largest_buy_share"),
        "recent_30s_tick_count": recent.get("tick_count"),
        "previous_30s_tick_count": previous.get("tick_count"),
        "recent_30s_volume": recent.get("volume"),
        "previous_30s_volume": previous.get("volume"),
        "recent_30s_volume_delta": recent.get("volume_delta"),
        "recent_to_previous_volume_ratio": ratio,
    }
    sustained_evaluable = (
        _window_complete(sustained, minimum_ticks=int(sustained_cfg["minimum_tick_count"]))
        and concentration.get("largest_buy_share") is not None
    )
    sustained_passed = bool(
        sustained_evaluable
        and float(sustained["volume_delta"]) >= float(sustained_cfg["minimum_volume_delta"])
        and float(sustained["buyer_tick_ratio"]) >= float(sustained_cfg["minimum_buyer_tick_ratio"])
        and float(concentration["largest_buy_share"]) <= float(sustained_cfg["maximum_largest_buy_share"])
    )
    minimum_recent_ticks = int(acceleration_cfg["minimum_ticks_each_window"])
    acceleration_evaluable = bool(
        int(recent.get("tick_count") or 0) >= minimum_recent_ticks
        and int(previous.get("tick_count") or 0) >= minimum_recent_ticks
        and recent.get("volume_delta") is not None
        and ratio is not None
    )
    acceleration_passed = bool(
        acceleration_evaluable
        and float(recent["volume_delta"]) >= float(acceleration_cfg["minimum_recent_volume_delta"])
        and float(ratio) >= float(acceleration_cfg["minimum_recent_to_previous_volume_ratio"])
    )
    required = (
        (sustained_evaluable, sustained_passed)
        if variant == "V1_SUSTAINED_DISTRIBUTED_BUYING"
        else (acceleration_evaluable, acceleration_passed)
        if variant == "V2_RECENT_VOLUME_ACCELERATION"
        else (
            sustained_evaluable and acceleration_evaluable,
            sustained_passed and acceleration_passed,
        )
    )
    evaluable, passed = required
    if not evaluable:
        reason = "VOLUME_GATE_DATA_INSUFFICIENT"
    elif passed:
        reason = "VOLUME_GATE_PASSED"
    elif variant == "V1_SUSTAINED_DISTRIBUTED_BUYING":
        reason = "SUSTAINED_DISTRIBUTED_BUYING_FAILED"
    elif variant == "V2_RECENT_VOLUME_ACCELERATION":
        reason = "RECENT_VOLUME_ACCELERATION_FAILED"
    else:
        failed = []
        if not sustained_passed:
            failed.append("SUSTAINED")
        if not acceleration_passed:
            failed.append("ACCELERATION")
        reason = "PLUS".join(failed) + "_FAILED"
    return {
        "evaluable": bool(evaluable),
        "passed": bool(passed),
        "reason": reason,
        **features,
    }


def _entry_fill(
    candidate: LiveSignal,
    data: Mapping[str, Any],
    scenario: str,
    capital_twd: int,
) -> tuple[datetime, float, int] | None:
    if scenario == "BASE":
        at = candidate.decision_time
        price = float(candidate.entry_price)
    elif scenario == "STRESS":
        target = candidate.decision_time + timedelta(seconds=5)
        row = next(
            (item for item in data["ticks"] if item["time"] >= target), None,
        )
        if row is None or not 0 <= (row["time"] - target).total_seconds() <= float(
            SPEC["maximum_tick_staleness_seconds"]
        ):
            return None
        at = row["time"]
        price = float(row["ask"])
        # The production proxy already crosses one adverse tick.  Stress adds
        # one more tick without claiming queue priority or a real fill.
        price += _tick_size(price)
        price += _tick_size(price)
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    quantity = math.floor(capital_twd / (price * 1000)) * 1000
    return (at, price, quantity) if quantity > 0 else None


def _trade_base(session: SessionData, variant: str, scenario: str) -> dict[str, Any]:
    return {
        "session_date": session.session_date,
        "source_run_id": session.manifest["run_id"],
        "source_manifest_hash": session.manifest["manifest_hash"],
        "variant": variant,
        "scenario": scenario,
        "status": "NO_TRADE",
        "stock_id": None,
        "stock_name": None,
        "signal_time": None,
        "entry_time": None,
        "entry_price": None,
        "quantity": 0,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "gross_pnl_twd": 0.0,
        "commission_twd": 0.0,
        "tax_twd": 0.0,
        "net_pnl_twd": 0.0,
        "mfe_net_pnl_twd": None,
        "mae_net_pnl_twd": None,
        "data_insufficient_rejections": 0,
        "gate_rejections": 0,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }


def replay_variant(
    session: SessionData,
    *,
    variant: str,
    scenario: str,
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    capital_twd = int(config["capital_twd"])
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    combined = {**session.candidates, **session.market}
    metadata = {
        symbol: str(data.get("meta", {}).get("stock_name", symbol))
        for symbol, data in combined.items()
    }
    engine = _ScenarioEngine(
        metadata,
        capital_twd=capital_twd,
        candidate_symbols=set(session.candidates),
        benchmark_symbol=benchmark,
        adverse_exit_ticks=(1 if scenario == "STRESS" else 0),
    )
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    signal_rows: list[dict[str, Any]] = []
    selected: LiveSignal | None = None
    selected_gate: dict[str, Any] | None = None
    for decision in _decision_times(
        session.session_date, str(SPEC["entry_start"]), str(SPEC["last_entry_time"]),
    ):
        _feed_until(engine, combined, tick_indexes, book_indexes, decision)
        candidate = engine.choose_entry(decision, allow_short=False)
        if candidate is None:
            continue
        gate = evaluate_volume_gate(
            session.candidates[candidate.stock_id], decision, variant, config,
        )
        signal_rows.append({
            "session_date": session.session_date,
            "variant": variant,
            "scenario": scenario,
            "signal_id": f"{session.session_date}-{candidate.stock_id}-{decision.strftime('%H%M%S')}",
            "stock_id": candidate.stock_id,
            "stock_name": candidate.stock_name,
            "decision_time": decision.isoformat(),
            "production_score": candidate.score,
            "production_volume_delta_60s": candidate.volume_delta,
            "production_large_trade_delta_60s": candidate.large_trade_delta,
            "production_book_imbalance": candidate.book_imbalance,
            "production_spread_bps": candidate.spread_bps,
            "gate_evaluable": gate["evaluable"],
            "gate_passed": gate["passed"],
            "gate_reason": gate["reason"],
            **{key: value for key, value in gate.items() if key not in {"evaluable", "passed", "reason"}},
            "selected_for_entry": bool(gate["passed"]),
        })
        if gate["passed"]:
            selected = candidate
            selected_gate = gate
            break
    base = _trade_base(session, variant, scenario)
    base["data_insufficient_rejections"] = sum(
        row["gate_reason"] == "VOLUME_GATE_DATA_INSUFFICIENT" for row in signal_rows
    )
    base["gate_rejections"] = sum(not row["gate_passed"] for row in signal_rows)
    if selected is None:
        return base, signal_rows
    fill = _entry_fill(
        selected, session.candidates[selected.stock_id], scenario, capital_twd,
    )
    if fill is None:
        return ({
            **base,
            "status": "UNFILLED_ENTRY_QUOTE_UNAVAILABLE",
            "stock_id": selected.stock_id,
            "stock_name": selected.stock_name,
            "signal_time": selected.decision_time.isoformat(),
        }, signal_rows)
    entry_time, entry_price, quantity = fill
    _feed_until(engine, combined, tick_indexes, book_indexes, entry_time)
    position = ManagedPosition(
        stock_id=selected.stock_id,
        stock_name=selected.stock_name,
        side="LONG",
        quantity=quantity,
        entry_price=entry_price,
        entry_order_id="RESEARCH_ONLY_NO_ORDER",
        entry_time=entry_time,
    )
    data = session.candidates[selected.stock_id]
    events: list[tuple[datetime, int, str, dict[str, Any] | None]] = []
    for row in data["ticks"][tick_indexes[selected.stock_id]:]:
        events.append((row["time"], 0, "tick", row))
    for row in data["books"][book_indexes[selected.stock_id]:]:
        events.append((row["time"], 1, "book", row))
    end_time = _parse_stamp(session.manifest["ended_at"])
    for decision in _decision_times(
        session.session_date, str(SPEC["entry_start"]), str(SPEC["hard_exit_time"]),
    ):
        if entry_time < decision <= end_time:
            events.append((decision, 2, "decision", None))
    events.sort(key=lambda item: (item[0], item[1]))
    exit_decision = None
    exit_time = None
    held_values: list[float] = []
    for at, _priority, kind, row in events:
        reversal = False
        if kind == "tick":
            assert row is not None
            _record_tick(engine, selected.stock_id, row)
        elif kind == "book":
            assert row is not None
            _record_book(engine, selected.stock_id, row)
        else:
            reversal = engine.opposite_signal(position, at)
            engine.last_decision = at
        quote = engine.safe_exit_quote(position, at, max_age_seconds=3.0)
        if quote is not None:
            held_values.append(float(engine.projected_net(position, quote.price)))
        proposed = engine.evaluate_exit(
            position, at, reversal=reversal, max_quote_age_seconds=3.0,
        )
        if proposed is not None:
            exit_decision = proposed
            exit_time = at
            break
    if exit_decision is None or exit_time is None:
        return ({
            **base,
            "status": "UNSCORABLE_EXIT_FRESH_QUOTE_MISSING",
            "stock_id": selected.stock_id,
            "stock_name": selected.stock_name,
            "signal_time": selected.decision_time.isoformat(),
            "entry_time": entry_time.isoformat(),
            "entry_price": entry_price,
            "quantity": quantity,
        }, signal_rows)
    gross, commission, tax, net = _projected_net_pnl(
        "LONG", entry_price, float(exit_decision.price), quantity,
    )
    return ({
        **base,
        "status": "SCORED_TRADE",
        "stock_id": selected.stock_id,
        "stock_name": selected.stock_name,
        "signal_time": selected.decision_time.isoformat(),
        "entry_time": entry_time.isoformat(),
        "entry_price": entry_price,
        "quantity": quantity,
        "exit_time": exit_time.isoformat(),
        "exit_price": float(exit_decision.price),
        "exit_reason": exit_decision.reason,
        "gross_pnl_twd": gross,
        "commission_twd": commission,
        "tax_twd": tax,
        "net_pnl_twd": net,
        "mfe_net_pnl_twd": max(held_values, default=float(net)),
        "mae_net_pnl_twd": min(held_values, default=float(net)),
        **{
            f"gate_{key}": value for key, value in (selected_gate or {}).items()
            if key not in {"evaluable", "passed", "reason"}
        },
    }, signal_rows)


def maximum_drawdown(pnl: Iterable[float]) -> float:
    equity = peak = drawdown = 0.0
    for value in pnl:
        equity += float(value)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 2)


def summarize(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scenario in SCENARIOS:
        for variant in VARIANTS:
            selected = [
                row for row in trades
                if row["scenario"] == scenario and row["variant"] == variant
            ]
            scored = [row for row in selected if row["status"] == "SCORED_TRADE"]
            pnl = [float(row["net_pnl_twd"]) for row in scored]
            winners = [value for value in pnl if value > 0]
            losers = [value for value in pnl if value < 0]
            net = sum(pnl)
            rows.append({
                "scenario": scenario,
                "variant": variant,
                "eligible_session_count": len(selected),
                "trade_count": len(scored),
                "trading_day_count": len({row["session_date"] for row in scored}),
                "winning_trades": len(winners),
                "losing_trades": len(losers),
                "win_rate": len(winners) / len(scored) if scored else None,
                "gross_profit_twd": sum(winners),
                "gross_loss_twd": sum(losers),
                "net_pnl_twd": net,
                "incremental_vs_flat_twd": net,
                "average_trade_twd": mean(pnl) if pnl else None,
                "median_trade_twd": median(pnl) if pnl else None,
                "profit_factor": (
                    sum(winners) / abs(sum(losers))
                    if losers else ("INFINITE" if winners else None)
                ),
                "maximum_drawdown_twd": maximum_drawdown(pnl),
                "worst_trade_twd": min(pnl) if pnl else None,
                "best_trade_twd": max(pnl) if pnl else None,
                "remove_best_trade_net_pnl_twd": net - max(pnl) if pnl else 0.0,
                "total_commission_twd": sum(float(row["commission_twd"]) for row in scored),
                "total_tax_twd": sum(float(row["tax_twd"]) for row in scored),
                "no_trade_days": sum(row["status"] == "NO_TRADE" for row in selected),
                "unfilled_count": sum(row["status"].startswith("UNFILLED") for row in selected),
                "unscorable_count": sum(row["status"].startswith("UNSCORABLE") for row in selected),
                "data_insufficient_rejections": sum(int(row["data_insufficient_rejections"]) for row in selected),
                "gate_rejections": sum(int(row["gate_rejections"]) for row in selected),
            })
    base_by_scenario = {
        row["scenario"]: row for row in rows
        if row["variant"] == "V0_CURRENT_PRODUCTION"
    }
    for row in rows:
        row["incremental_vs_v0_twd"] = (
            float(row["net_pnl_twd"])
            - float(base_by_scenario[row["scenario"]]["net_pnl_twd"])
        )
    return rows


def build_report() -> dict[str, Any]:
    config = load_config()
    sessions, inventory, unscorable = discover_sessions()
    trades: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    for session in sessions:
        for scenario in SCENARIOS:
            for variant in VARIANTS:
                trade, seen = replay_variant(
                    session, variant=variant, scenario=scenario, config=config,
                )
                trades.append(trade)
                signals.extend(seen)
    experiments = summarize(trades)
    daily = [{
        "session_date": row["session_date"],
        "scenario": row["scenario"],
        "variant": row["variant"],
        "status": row["status"],
        "stock_id": row["stock_id"],
        "net_pnl_twd": row["net_pnl_twd"],
        "trade_count": int(row["status"] == "SCORED_TRADE"),
        "data_insufficient_rejections": row["data_insufficient_rejections"],
        "gate_rejections": row["gate_rejections"],
    } for row in trades]
    return {
        "analysis_id": config["analysis_id"],
        "config": config,
        "config_hash": config_hash(config),
        "spec_hash": sha256_file(SPEC_PATH),
        "eligible_dates": [session.session_date for session in sessions],
        "existing_dates_status": config["existing_dates_status"],
        "data_inventory": inventory,
        "unscorable": unscorable,
        "experiments": experiments,
        "trades": trades,
        "signals": signals,
        "daily_results": daily,
        "production_contract": {
            "signal_spec": SPEC,
            "market_policy": LONG_MARKET_REGIME_POLICY,
            "anti_chase_policy": ANTI_CHASE_ENTRY_POLICY,
            "exit_policy": LIVE_EXIT_POLICY,
        },
        "source_hashes": {
            "analysis.py": sha256_file(MODULE_DIR / "analysis.py"),
            "main.py": sha256_file(MODULE_DIR / "main.py"),
            "config.json": sha256_file(CONFIG_PATH),
            "EXPERIMENT_SPEC.md": sha256_file(SPEC_PATH),
            "entry_quality_helper.py": sha256_file(
                MODULE_DIR.parent / "entry_quality_edge_study_v01" / "analysis.py"
            ),
            "production_strategy.py": sha256_file(
                MODULE_DIR.parent / "yuanta_live_runtime_v01" / "strategy.py"
            ),
        },
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
        "production_behavior_changed": False,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    flat = [{
        key: json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, (dict, list, tuple)) else value
        for key, value in row.items()
    } for row in rows]
    fields: list[str] = []
    for row in flat:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(flat)


def _fmt(value: Any) -> str:
    if value is None:
        return "NA"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, str):
        try:
            return f"{float(value):.2f}"
        except ValueError:
            return value
    return str(value)


def markdown_report(report: Mapping[str, Any]) -> str:
    rows = report["experiments"]
    base = next(row for row in rows if row["scenario"] == "BASE" and row["variant"] == "V0_CURRENT_PRODUCTION")
    lines = [
        "# 成交量與買盤連續性研究", "",
        f"- 可評估日期：{len(report['eligible_dates'])}日（{', '.join(report['eligible_dates']) or '無'}）",
        f"- 既有日期狀態：`{report['existing_dates_status']}`",
        "- 模式：`SHADOW_ONLY`／`NO_LIVE_ORDER`，actual_orders=0、actual_fills=0。",
        "- 正式策略、runtime、PROD與排程未修改。", "",
        "## 結果", "",
        "|情境|版本|交易|淨損益|相對V0|勝率|PF|最大回撤|移除最佳筆|資料不足拒絕|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"|{row['scenario']}|{row['variant']}|{row['trade_count']}|"
            f"{_fmt(row['net_pnl_twd'])}|{_fmt(row['incremental_vs_v0_twd'])}|"
            f"{_fmt(row['win_rate'])}|{_fmt(row['profit_factor'])}|"
            f"{_fmt(row['maximum_drawdown_twd'])}|{_fmt(row['remove_best_trade_net_pnl_twd'])}|"
            f"{row['data_insufficient_rejections']}|"
        )
    base_positive = float(base["net_pnl_twd"]) > 0
    by_key = {(row["scenario"], row["variant"]): row for row in rows}
    v2_base = by_key[("BASE", "V2_RECENT_VOLUME_ACCELERATION")]
    v2_stress = by_key[("STRESS", "V2_RECENT_VOLUME_ACCELERATION")]
    v0_stress = by_key[("STRESS", "V0_CURRENT_PRODUCTION")]
    base_trades = {
        (row["session_date"], row["stock_id"], row["signal_time"]): row
        for row in report["trades"]
        if row["scenario"] == "BASE"
        and row["variant"] == "V0_CURRENT_PRODUCTION"
        and row["status"] == "SCORED_TRADE"
    }
    lines.extend(["", "## 判讀", ""])
    lines.extend([
        f"- V0本身成本後損益{'為正' if base_positive else '仍為負'}；空手基準固定為0元。",
        f"- V0基礎代理為{_fmt(base['net_pnl_twd'])}元，但壓力成交為{_fmt(v0_stress['net_pnl_twd'])}元，"
        f"且基礎結果移除最佳一筆只剩{_fmt(base['remove_best_trade_net_pnl_twd'])}元，優勢脆弱。",
        f"- V2基礎為{_fmt(v2_base['net_pnl_twd'])}元，比V0少{_fmt(abs(float(v2_base['incremental_vs_v0_twd'])))}元；"
        f"壓力成交為{_fmt(v2_stress['net_pnl_twd'])}元，相對V0壓力改善{_fmt(v2_stress['incremental_vs_v0_twd'])}元。"
        f"這只是{v2_base['trade_count']}筆／壓力{v2_stress['trade_count']}筆的候選現象。",
        "- V1只擋掉一筆獲利交易，V3在基礎情境轉負；兩者沒有繼續擴充的證據。",
        "- 交易變少不等於策略改善。`signals.csv`保留每次被擋候選及當下可見特徵。",
        "- 現有日期不是新的樣本外。只有凍結後新資料達10筆／5日且壓力情境仍成立，才可提出UAT候選。",
        "- 封存行情沒有broker fill stream，完整成交只是明示的研究代理；成交量股／張單位仍未完全證實。",
    ])
    lines.extend(["", "## 交易取捨", ""])
    for variant in VARIANTS[1:]:
        selected = {
            (row["session_date"], row["stock_id"], row["signal_time"]): row
            for row in report["trades"]
            if row["scenario"] == "BASE" and row["variant"] == variant
            and row["status"] == "SCORED_TRADE"
        }
        removed = [base_trades[key] for key in sorted(base_trades.keys() - selected.keys())]
        added = [selected[key] for key in sorted(selected.keys() - base_trades.keys())]
        removed_text = ", ".join(
            f"{row['session_date']} {row['stock_id']} ({_fmt(row['net_pnl_twd'])}元)"
            for row in removed
        ) or "無"
        added_text = ", ".join(
            f"{row['session_date']} {row['stock_id']} ({_fmt(row['net_pnl_twd'])}元)"
            for row in added
        ) or "無"
        lines.append(f"- `{variant}`：擋掉V0交易 {removed_text}；新增後續交易 {added_text}。")
    return "\n".join(lines) + "\n"


def write_results(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_csv(output_dir / "data_audit.csv", report["data_inventory"])
    _write_csv(output_dir / "experiments.csv", report["experiments"])
    _write_csv(output_dir / "trades.csv", report["trades"])
    _write_csv(output_dir / "signals.csv", report["signals"])
    _write_csv(output_dir / "daily_results.csv", report["daily_results"])
    _write_csv(output_dir / "unscorable.csv", report["unscorable"])
    (output_dir / "RESEARCH_REPORT.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    manifest = {
        key: value for key, value in report.items()
        if key not in {"data_inventory", "experiments", "trades", "signals", "daily_results", "unscorable"}
    }
    manifest["artifacts"] = {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir()) if path.is_file()
    }
    manifest["rerun_command"] = (
        "cd stock-strategy && python3 -m volume_flow_entry_study_v01.main "
        "--output-dir /path/to/new-empty-directory"
    )
    payload = {**manifest}
    payload["manifest_hash"] = hashlib.sha256(canonical_bytes(payload)).hexdigest()
    (output_dir / "run_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {path.name: sha256_file(path) for path in sorted(output_dir.iterdir()) if path.is_file()}


def refresh_existing_presentation(output_dir: Path) -> dict[str, str]:
    """Refresh report/source hashes without recomputing frozen performance.

    This is intentionally limited to presentation artifacts.  It requires the
    already-produced numerical CSVs and never reads quote archives.
    """
    manifest_path = output_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    def read_csv(name: str) -> list[dict[str, Any]]:
        with (output_dir / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    report = {
        **manifest,
        "experiments": read_csv("experiments.csv"),
        "trades": read_csv("trades.csv"),
    }
    (output_dir / "RESEARCH_REPORT.md").write_text(
        markdown_report(report), encoding="utf-8",
    )
    manifest["source_hashes"] = {
        "analysis.py": sha256_file(MODULE_DIR / "analysis.py"),
        "main.py": sha256_file(MODULE_DIR / "main.py"),
        "config.json": sha256_file(CONFIG_PATH),
        "EXPERIMENT_SPEC.md": sha256_file(SPEC_PATH),
        "entry_quality_helper.py": sha256_file(
            MODULE_DIR.parent / "entry_quality_edge_study_v01" / "analysis.py"
        ),
        "production_strategy.py": sha256_file(
            MODULE_DIR.parent / "yuanta_live_runtime_v01" / "strategy.py"
        ),
    }
    manifest["artifacts"] = {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "run_manifest.json"
    }
    manifest.pop("manifest_hash", None)
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        path.name: sha256_file(path)
        for path in sorted(output_dir.iterdir()) if path.is_file()
    }
