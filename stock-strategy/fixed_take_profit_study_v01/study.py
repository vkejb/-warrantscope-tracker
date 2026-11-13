"""Causal, broker-free comparison of fixed and MFE profit exits."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC, _archived_exchange_time, _projected_net_pnl,
)
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    _feed_until, _parse_stamp, _record_book, _record_tick,
)
from yuanta_live_runtime_v01.strategy import (
    LIVE_EXIT_POLICY, LiveDirectionEngine, ManagedPosition,
)


MODULE_DIR = Path(__file__).resolve().parent
STOCK_STRATEGY_DIR = MODULE_DIR.parent
RUNS_DIR = STOCK_STRATEGY_DIR / "yuanta_intraday_shadow_v01" / "runtime" / "runs"
PAPER_DIR = STOCK_STRATEGY_DIR / "expanded_shadow_universe_v01" / "runtime" / "days"
SPEC_PATH = MODULE_DIR / "EXPERIMENT_SPEC.md"
RESULTS_DIR = MODULE_DIR / "results"
ANALYSIS_ID = "FIXED_TAKE_PROFIT_STUDY_V0_1"
RISK_TWD = 3500.0
VARIANTS = (
    "CURRENT_MFE_V1",
    "FIXED_NET_1R",
    "FIXED_NET_1_5R",
    "HYBRID_EARLY_FLOOR",
)
SCENARIOS = ("BASE", "EXIT_DELAY_1S")
TARGETS = {"FIXED_NET_1R": RISK_TWD, "FIXED_NET_1_5R": 1.5 * RISK_TWD}
HYBRID_ARM = 0.75 * RISK_TWD
HYBRID_FLOOR = 0.30 * RISK_TWD


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _verify_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    expected = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
    if expected != manifest.get("manifest_hash"):
        raise RuntimeError(f"manifest hash mismatch: {path}")
    for name, digest in manifest.get("artifacts", {}).items():
        artifact = path.parent / name
        if not artifact.is_file() or sha256_file(artifact) != digest:
            raise RuntimeError(f"paper artifact hash mismatch: {artifact}")
    return manifest


def discover_days() -> list[dict[str, Any]]:
    rows = []
    seen: set[str] = set()
    for path in sorted(PAPER_DIR.glob("*/*/manifest.json")):
        manifest = _verify_manifest(path)
        day = str(manifest["session_date"])
        if day in seen:
            raise RuntimeError(f"ambiguous duplicate expanded paper date: {day}")
        seen.add(day)
        trades = list(_read_jsonl(path.parent / "paper_trades.jsonl"))
        production = [
            row for row in trades
            if row.get("strategy_variant") == "PRODUCTION_ANTI_CHASE"
        ]
        if len(production) > 1:
            raise RuntimeError(f"more than one production paper entry: {day}")
        run_dir = RUNS_DIR / str(manifest["source_run_id"])
        source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        source_unsigned = {key: value for key, value in source.items() if key != "manifest_hash"}
        if hashlib.sha256(canonical_bytes(source_unsigned)).hexdigest() != source.get("manifest_hash"):
            raise RuntimeError(f"source manifest hash mismatch: {run_dir}")
        if manifest.get("source_manifest_hash") != source.get("manifest_hash"):
            raise RuntimeError(f"paper/source identity mismatch: {day}")
        rows.append({
            "session_date": day,
            "paper_manifest_path": str(path.resolve()),
            "paper_manifest_hash": manifest["manifest_hash"],
            "paper_contract_hash": manifest["paper_contract_hash"],
            "source_run_id": source["run_id"],
            "source_manifest_hash": source["manifest_hash"],
            "run_dir": run_dir,
            "ended_at": source["ended_at"],
            "trade": production[0] if production else None,
        })
    contracts = {row["paper_contract_hash"] for row in rows}
    if len(contracts) != 1:
        raise RuntimeError("paper days do not share one frozen contract")
    return rows


def _numbers(values: Any) -> list[float]:
    output = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            output.append(number)
    return output


def _selected_market_data(run_dir: Path, symbol: str) -> dict[str, Any]:
    """Load only the sealed selected symbol while scanning immutable archives."""
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    tick_name = "expanded_ticks.jsonl.gz"
    book_name = "expanded_books.jsonl.gz"
    for name in (tick_name, book_name):
        if name not in manifest.get("artifacts", {}) or not (run_dir / name).is_file():
            raise RuntimeError(f"missing expanded artifact: {run_dir / name}")
    needle = f'"stock_id":"{symbol}"'.encode()
    ticks: list[dict[str, Any]] = []
    with gzip.open(run_dir / tick_name, "rb") as handle:
        for raw in handle:
            if needle not in raw:
                continue
            row = json.loads(raw)
            try:
                received = _parse_stamp(row["received_at"])
                item = {
                    "time": received,
                    "received_at": received,
                    "exchange_time": _archived_exchange_time(row.get("quote_time"), received),
                    "price": float(row["deal_price"]),
                    "volume": float(row["deal_volume"]),
                    "bid": float(row["buy_price"]),
                    "ask": float(row["sell_price"]),
                    "flag": str(row.get("in_out_flag", "")),
                    "serial": int(row.get("serial_no", 0)),
                }
            except (KeyError, TypeError, ValueError):
                continue
            if min(item["price"], item["bid"], item["ask"]) > 0 and item["volume"] >= 0:
                ticks.append(item)
    books: list[dict[str, Any]] = []
    with gzip.open(run_dir / book_name, "rb") as handle:
        for raw in handle:
            if needle not in raw:
                continue
            row = json.loads(raw)
            buys, sells = _numbers(row.get("buy_volumes")), _numbers(row.get("sell_volumes"))
            buy_prices, sell_prices = _numbers(row.get("buy_prices")), _numbers(row.get("sell_prices"))
            if not buys or not sells or not buy_prices or not sell_prices:
                continue
            if buy_prices[0] <= 0 or sell_prices[0] <= 0:
                continue
            books.append({
                "time": _parse_stamp(row["received_at"]),
                "buy_volume": sum(buys), "sell_volume": sum(sells),
                "best_bid": buy_prices[0], "best_ask": sell_prices[0],
            })
    ticks.sort(key=lambda row: (row["time"], row["serial"]))
    books.sort(key=lambda row: row["time"])
    if not ticks or not books:
        raise RuntimeError(f"selected symbol data incomplete: {symbol}/{run_dir.name}")
    return {
        "ticks": ticks, "books": books,
        "tick_times": [row["time"] for row in ticks],
        "book_times": [row["time"] for row in books],
        "meta": {"stock_name": symbol},
    }


def _exit_reason(
    engine: LiveDirectionEngine,
    position: ManagedPosition,
    at: datetime,
    variant: str,
    reversal: bool,
    mfe_net: float,
    hybrid_armed: bool,
) -> tuple[str | None, float | None, float, bool]:
    quote = engine.safe_exit_quote(position, at, max_age_seconds=3.0)
    if quote is None:
        return None, None, mfe_net, hybrid_armed
    price = quote.price
    projected = engine.projected_net(position, price)
    mfe_net = max(mfe_net, projected)
    hybrid_armed = hybrid_armed or mfe_net >= HYBRID_ARM
    reason = None
    if variant in {"CURRENT_MFE_V1", "HYBRID_EARLY_FLOOR"}:
        engine._observe_mfe(  # research parity with the production state machine
            position, price=price, projected_net_pnl=projected, at=at,
        )
    if projected <= -RISK_TWD:
        reason = "STOP_LOSS"
    elif variant in {"CURRENT_MFE_V1", "HYBRID_EARLY_FLOOR"} and engine._mfe_floor_breached(position, price):
        reason = "MFE_PROFIT_PROTECTION"
    elif variant == "HYBRID_EARLY_FLOOR" and hybrid_armed and projected <= HYBRID_FLOOR:
        reason = "EARLY_NET_PROFIT_FLOOR"
    elif variant in TARGETS and projected >= TARGETS[variant]:
        reason = "FIXED_NET_TAKE_PROFIT"
    elif reversal:
        reason = "SIGNAL_REVERSAL"
    elif at.time() >= LiveDirectionEngine._clock(SPEC["hard_exit_time"]):
        reason = "HARD_EXIT"
    return reason, price, mfe_net, hybrid_armed


def simulate_exit(
    data: dict[str, Any], trade: dict[str, Any], end: datetime,
    variant: str, scenario: str,
) -> dict[str, Any]:
    if variant not in VARIANTS or scenario not in SCENARIOS:
        raise ValueError("unknown variant/scenario")
    symbol = str(trade["stock_id"])
    entry = _parse_stamp(trade["entry_time"])
    engine = LiveDirectionEngine(
        {symbol: str(trade.get("stock_name") or symbol)},
        capital_twd=190_000, candidate_symbols={symbol}, benchmark_symbol=None,
    )
    combined = {symbol: data}
    tick_indexes = {symbol: 0}
    book_indexes = {symbol: 0}
    _feed_until(engine, combined, tick_indexes, book_indexes, entry)
    position = ManagedPosition(
        symbol, str(trade.get("stock_name") or symbol), "LONG",
        int(trade["quantity"]), float(trade["entry_price"]),
        "RESEARCH_ONLY_NO_ORDER", entry,
    )
    events: list[tuple[datetime, int, str, dict[str, Any] | None]] = []
    events += [(row["time"], 0, "tick", row) for row in data["ticks"][tick_indexes[symbol]:]]
    events += [(row["time"], 1, "book", row) for row in data["books"][book_indexes[symbol]:]]
    decision = entry + timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    while decision <= end:
        events.append((decision, 2, "decision", None))
        decision += timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    events.sort(key=lambda item: (item[0], item[1]))
    entry_net = engine.projected_net(position, position.entry_price)
    mfe_net = entry_net
    mae_net = entry_net
    hybrid_armed = False
    trigger: tuple[datetime, str] | None = None
    delay = 0 if scenario == "BASE" else 1
    for at, _priority, kind, row in events:
        reversal = False
        if kind == "tick":
            assert row is not None
            _record_tick(engine, symbol, row)
        elif kind == "book":
            assert row is not None
            _record_book(engine, symbol, row)
        else:
            reversal = engine.opposite_signal(position, at)
            engine.last_decision = at
        quote = engine.safe_exit_quote(position, at, max_age_seconds=3.0)
        if quote is not None:
            current = engine.projected_net(position, quote.price)
            mfe_net, mae_net = max(mfe_net, current), min(mae_net, current)
        if trigger is not None:
            if at < trigger[0] + timedelta(seconds=delay) or quote is None:
                continue
            reason, trigger_at = trigger[1], trigger[0]
            price = quote.price
        else:
            reason, price, mfe_net, hybrid_armed = _exit_reason(
                engine, position, at, variant, reversal, mfe_net, hybrid_armed,
            )
            if reason is None or price is None:
                continue
            trigger_at = at
            if delay:
                trigger = (at, reason)
                continue
        gross, commission, tax, net = _projected_net_pnl(
            "LONG", position.entry_price, price, position.quantity,
        )
        return {
            "status": "SCORED", "variant": variant, "scenario": scenario,
            "session_date": str(trade["session_date"]), "stock_id": symbol,
            "stock_name": trade.get("stock_name"), "entry_time": trade["entry_time"],
            "entry_price": position.entry_price, "quantity": position.quantity,
            "exit_trigger_time": trigger_at.isoformat(), "exit_time": at.isoformat(),
            "exit_price": price, "exit_reason": reason,
            "trigger_to_fill_delay_seconds": (at - trigger_at).total_seconds(),
            "gross_pnl": gross, "commission": commission, "sell_tax": tax,
            "net_pnl": net, "mfe_net_pnl": mfe_net, "mae_net_pnl": mae_net,
            "profit_giveback_twd": max(0.0, mfe_net - net),
            "holding_seconds": (at - entry).total_seconds(),
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        }
    return {
        "status": "UNSCORABLE_EXIT_FRESH_QUOTE_MISSING", "variant": variant,
        "scenario": scenario, "session_date": str(trade["session_date"]),
        "stock_id": symbol, "stock_name": trade.get("stock_name"),
        "entry_time": trade["entry_time"], "entry_price": position.entry_price,
        "quantity": position.quantity, "net_pnl": None, "mfe_net_pnl": mfe_net,
        "mae_net_pnl": mae_net, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0,
    }


def _maximum_drawdown(daily: list[float], initial: float = 190_000.0) -> float:
    equity = peak = initial
    drawdown = 0.0
    for value in daily:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(rows: list[dict[str, Any]], all_dates: list[str]) -> dict[str, Any]:
    scored = [row for row in rows if row["status"] == "SCORED"]
    values = [float(row["net_pnl"]) for row in scored]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    by_date = {str(row["session_date"]): float(row["net_pnl"]) for row in scored}
    daily = [by_date.get(day, 0.0) for day in all_dates]
    best = max(values) if values else None
    return {
        "trade_count": len(rows), "scored_trade_count": len(scored),
        "unscorable_trade_count": len(rows) - len(scored),
        "winning_trades": len(wins), "losing_trades": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "net_pnl_twd": sum(values),
        "mean_trade_twd": mean(values) if values else None,
        "median_trade_twd": median(values) if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else (math.inf if wins else None),
        "maximum_drawdown_twd": _maximum_drawdown(daily),
        "best_trade_twd": best,
        "net_pnl_excluding_best_trade_twd": sum(values) - best if best is not None else None,
        "average_mfe_twd": mean(float(row["mfe_net_pnl"]) for row in scored) if scored else None,
        "average_mae_twd": mean(float(row["mae_net_pnl"]) for row in scored) if scored else None,
        "total_profit_giveback_twd": sum(float(row["profit_giveback_twd"]) for row in scored),
        "average_holding_seconds": mean(float(row["holding_seconds"]) for row in scored) if scored else None,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _report(summary: dict[str, Any]) -> str:
    lines = [
        "# 固定停利與移動停利研究", "",
        f"- 日期：{summary['date_start']}～{summary['date_end']}（{summary['session_count']} 個完整封存日）",
        f"- 固定進場交易：{summary['entry_trade_count']} 筆；無進場日：{summary['no_entry_day_count']} 日",
        f"- 現行 MFE 封存對帳：{summary['baseline_parity_count']}/{summary['entry_trade_count']} 筆完全一致；最大損益差 {summary['baseline_max_abs_pnl_delta_twd']:.0f} 元",
        "- 狀態：SHADOW_ONLY / NO_LIVE_ORDER / STRICT_EXECUTABLE=NA", "",
        "| 情境 | 出場 | 筆數 | 淨損益 | 勝率 | PF | 最大回撤 | 扣最佳筆 | 回吐 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary["experiments"]:
        pf = "NA" if row["profit_factor"] is None else ("INF" if math.isinf(row["profit_factor"]) else f"{row['profit_factor']:.3f}")
        wr = "NA" if row["win_rate"] is None else f"{100*row['win_rate']:.1f}%"
        lines.append(
            f"| {row['scenario']} | {row['variant']} | {row['scored_trade_count']} | "
            f"{row['net_pnl_twd']:.0f} | {wr} | {pf} | {row['maximum_drawdown_twd']:.0f} | "
            f"{row['net_pnl_excluding_best_trade_twd']:.0f} | {row['total_profit_giveback_twd']:.0f} |"
        )
    base = next(row for row in summary["experiments"] if row["scenario"] == "BASE" and row["variant"] == "CURRENT_MFE_V1")
    challengers = [row for row in summary["experiments"] if row["scenario"] == "BASE" and row["variant"] != "CURRENT_MFE_V1"]
    best = max(challengers, key=lambda row: row["net_pnl_twd"])
    lines += ["", "## 判讀", ""]
    lines.append(
        f"BASE 中表現最好的替代方案為 `{best['variant']}`，相對現行 MFE 的成本後增量為 "
        f"{best['net_pnl_twd'] - base['net_pnl_twd']:.0f} 元。"
    )
    lines += [
        f"`FIXED_NET_1R` 的最大回撤比現行少 {base['maximum_drawdown_twd'] - next(row for row in challengers if row['variant'] == 'FIXED_NET_1R')['maximum_drawdown_twd']:.0f} 元；"
        f"`FIXED_NET_1_5R` 的淨損益雖最好，但相對 1R 只多 {best['net_pnl_twd'] - next(row for row in challengers if row['variant'] == 'FIXED_NET_1R')['net_pnl_twd']:.0f} 元，不能稱 1.5R 已勝出。",
        "固定停利是否較好，必須同時看壓力情境、最大回撤、扣除最佳筆與被提前截斷的大賺交易；本批資料已被觀察，只能視為探索結果。",
        "研究程式沒有券商連線、沒有委託，亦未修改正式出場規則。", "",
    ]
    return "\n".join(lines)


def run(output_root: Path = RESULTS_DIR) -> Path:
    days = discover_days()
    spec_hash = sha256_file(SPEC_PATH)
    code_hash = sha256_file(Path(__file__))
    identity = {
        "analysis_id": ANALYSIS_ID, "spec_hash": spec_hash, "code_hash": code_hash,
        "sources": {row["session_date"]: row["paper_manifest_hash"] for row in days},
    }
    run_id = hashlib.sha256(canonical_bytes(identity)).hexdigest()[:16]
    output = output_root / run_id
    if output.exists():
        raise RuntimeError(f"immutable result already exists: {output}")
    output.mkdir(parents=True)
    trades: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    for day in days:
        trade = day["trade"]
        source_rows.append({key: value for key, value in day.items() if key not in {"trade", "run_dir"}})
        if trade is None:
            continue
        data = _selected_market_data(day["run_dir"], str(trade["stock_id"]))
        end = _parse_stamp(day["ended_at"])
        for scenario in SCENARIOS:
            for variant in VARIANTS:
                result = simulate_exit(data, trade, end, variant, scenario)
                if scenario == "BASE" and variant == "CURRENT_MFE_V1":
                    result["archived_net_pnl"] = float(trade["net_pnl"])
                    result["archived_exit_reason"] = str(trade["exit_reason"])
                    result["baseline_pnl_delta_twd"] = (
                        float(result["net_pnl"]) - float(trade["net_pnl"])
                        if result.get("net_pnl") is not None else None
                    )
                    result["baseline_exit_reason_match"] = (
                        result.get("exit_reason") == trade.get("exit_reason")
                    )
                trades.append(result)
    dates = [row["session_date"] for row in days]
    experiments = []
    for scenario in SCENARIOS:
        for variant in VARIANTS:
            rows = [row for row in trades if row["scenario"] == scenario and row["variant"] == variant]
            metric = _metrics(rows, dates)
            experiments.append({"scenario": scenario, "variant": variant, **metric})
    baseline = next(row for row in experiments if row["scenario"] == "BASE" and row["variant"] == "CURRENT_MFE_V1")
    for row in experiments:
        row["incremental_net_pnl_vs_current_base_twd"] = row["net_pnl_twd"] - baseline["net_pnl_twd"]
    daily = []
    for day in dates:
        for scenario in SCENARIOS:
            for variant in VARIANTS:
                row = next((item for item in trades if item["session_date"] == day and item["scenario"] == scenario and item["variant"] == variant), None)
                daily.append({
                    "session_date": day, "scenario": scenario, "variant": variant,
                    "trade_count": int(row is not None),
                    "status": row["status"] if row else "NO_ENTRY",
                    "net_pnl_twd": row.get("net_pnl") if row else 0,
                })
    parity = [
        row for row in trades
        if row["scenario"] == "BASE" and row["variant"] == "CURRENT_MFE_V1"
    ]
    summary = {
        "analysis_id": ANALYSIS_ID, "spec_hash": spec_hash,
        "date_start": min(dates), "date_end": max(dates),
        "session_count": len(days),
        "entry_trade_count": sum(row["trade"] is not None for row in days),
        "no_entry_day_count": sum(row["trade"] is None for row in days),
        "paper_contract_hash": days[0]["paper_contract_hash"],
        "baseline_parity_count": sum(
            row.get("baseline_pnl_delta_twd") == 0
            and row.get("baseline_exit_reason_match") is True
            for row in parity
        ),
        "baseline_max_abs_pnl_delta_twd": max(
            (abs(float(row["baseline_pnl_delta_twd"])) for row in parity),
            default=0.0,
        ),
        "experiments": experiments, "actual_orders": 0, "actual_fills": 0,
        "broker_order_calls": 0, "strict_executable": "NA",
        "interpretation": "KNOWN_ARCHIVE_EXPLORATORY_NOT_LIVE_APPROVAL",
    }
    (output / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
    _write_csv(output / "experiments.csv", experiments)
    _write_csv(output / "trades.csv", trades)
    _write_csv(output / "daily_results.csv", daily)
    _write_csv(output / "sources.csv", source_rows)
    (output / "REPORT.md").write_text(_report(summary), encoding="utf-8")
    artifacts = {
        name: sha256_file(output / name)
        for name in ("summary.json", "experiments.csv", "trades.csv", "daily_results.csv", "sources.csv", "REPORT.md")
    }
    manifest = {
        **identity, "run_id": run_id, "artifacts": artifacts,
        "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        "reproduce": "PYTHONPATH=stock-strategy python3 -m fixed_take_profit_study_v01.main",
    }
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (output / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    return output
