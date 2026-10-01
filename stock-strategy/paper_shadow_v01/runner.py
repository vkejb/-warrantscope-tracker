"""Post-session paper replay using the production long-only signal engine."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Iterable

from mfe_profit_protection_study_v01.analysis import ResearchTrade, derive_initial_stop_price
from paper_shadow_v01.buffered_exit import (
    POLICIES as BUFFERED_EXIT_POLICIES,
    ExistingExit,
    result_dict as buffered_result_dict,
    simulate_buffered_exit,
)
from trade_path_diagnostics_v01.analysis import Outcome
from trade_path_diagnostics_v01.early_failure import (
    TradeCase,
    _grid_rows,
    _impact_rows,
    build_checkpoint_rows,
)
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import (
    SPEC,
    _archived_exchange_time,
    _decision_times,
    _exit_quote,
    _projected_net_pnl,
    _tick_size,
    load_session,
)
from yuanta_intraday_shadow_v01.exit_parameter_sweep import PathPoint
from yuanta_intraday_shadow_v01.live_parity_backtest import (
    EXECUTION_MODEL,
    _feed_until,
    _parse_stamp,
    _record_book,
    _record_tick,
    _validate_full_session_coverage,
)
from yuanta_live_runtime_v01.strategy import (
    ANTI_CHASE_ENTRY_POLICY,
    LIVE_EXIT_POLICY,
    LONG_MARKET_REGIME_POLICY,
    LiveDirectionEngine,
    ManagedPosition,
)


ANALYSIS_ID = "BACKGROUND_PAPER_SHADOW_V0_1"
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
PAPER_CONFIRMATION_POLICY = {
    "policy_id": "ANTI_CHASE_60S_CONFIRMATION_SHADOW_V1",
    "delay_seconds": 60,
    "minimum_directional_volume_delta": 0.10,
    "minimum_directional_large_trade_delta": 0.0,
    "requires_breakout_held": True,
    "rechecks_anti_chase": True,
}
PAPER_CONTRACT = {
    "analysis_id": ANALYSIS_ID,
    "mode": "POST_SESSION_CAUSAL_PAPER_REPLAY",
    "entry_engine": "PRODUCTION_LIVE_DIRECTION_ENGINE",
    "direction": "LONG_ONLY",
    "benchmark": "0050",
    "capital_twd": 190_000,
    "maximum_concurrent_positions": 1,
    "maximum_trades_per_session_per_variant": 1,
    "paper_variants": [
        "PRODUCTION_ANTI_CHASE",
        "ANTI_CHASE_PLUS_60S_CONFIRMATION",
        "RECOVERY_NET_MFE_BUFFER_0_30_SHADOW",
        "RECOVERY_NET_MFE_BUFFER_0_40_SHADOW",
    ],
    "production_anti_chase_policy": ANTI_CHASE_ENTRY_POLICY,
    "confirmation_60s": PAPER_CONFIRMATION_POLICY,
    "execution_model": EXECUTION_MODEL,
    "exit_policy": LIVE_EXIT_POLICY,
    "market_regime_policy": LONG_MARKET_REGIME_POLICY,
    "early_failure_mode": {
        "production_tracks": "OBSERVE_ONLY_DO_NOT_EXIT",
        "buffered_exit_tracks": "ONE_TIME_120_SECOND_RECOVERY_AWARE_SHADOW_EXIT",
    },
    "early_failure_checkpoints_minutes": [5, 10, 15],
    "buffered_exit_policy": {
        "mode": "POST_SESSION_PAPER_ONLY",
        "activation_r": 0.75,
        "initial_lock_r_variants": [0.30, 0.40],
        "r_basis": "NET_PNL_AFTER_FEES_AND_TAX",
        "retain_1_5": 0.50,
        "retain_2_0": 0.60,
        "retain_3_0": 0.70,
        "hard_stop_net_twd": 3500.0,
    },
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_connections": 0,
}
PAPER_CONTRACT_HASH = hashlib.sha256(canonical_bytes(PAPER_CONTRACT)).hexdigest()


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _numbers(values: Iterable[Any]) -> list[float]:
    output = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            output.append(number)
    return output


def _load_market_context(run_dir: Path, source_manifest: dict) -> dict[str, dict]:
    artifacts = source_manifest.get("artifacts", {})
    tick_name = next(
        (name for name in ("market_context_ticks.jsonl.gz", "market_context_ticks.jsonl") if name in artifacts),
        None,
    )
    book_name = next(
        (name for name in ("market_context_books.jsonl.gz", "market_context_books.jsonl") if name in artifacts),
        None,
    )
    if tick_name is None or book_name is None:
        raise RuntimeError("paper replay requires archived 0050 tick and book artifacts")
    for name in (tick_name, book_name):
        if sha256_file(run_dir / name) != artifacts[name]:
            raise RuntimeError(f"market context artifact hash mismatch: {name}")
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    metadata = {
        str(row["stock_id"]): str(row.get("stock_name") or row["stock_id"])
        for row in watchlist.get("market_context", [])
    }
    stocks: dict[str, dict] = {}
    for row in _read_jsonl(run_dir / tick_name):
        try:
            received_at = _parse_stamp(row["received_at"])
            item = {
                "time": received_at,
                "received_at": received_at,
                "exchange_time": _archived_exchange_time(
                    row.get("quote_time"), received_at
                ),
                "price": float(row["deal_price"]),
                "volume": float(row["deal_volume"]),
                "bid": float(row["buy_price"]),
                "ask": float(row["sell_price"]),
                "flag": str(row.get("in_out_flag", "")),
                "serial": int(row.get("serial_no", 0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if min(item["price"], item["bid"], item["ask"]) <= 0 or item["volume"] < 0:
            continue
        stocks.setdefault(str(row["stock_id"]), {"ticks": [], "books": []})["ticks"].append(item)
    for row in _read_jsonl(run_dir / book_name):
        buys, sells = _numbers(row.get("buy_volumes", [])), _numbers(row.get("sell_volumes", []))
        buy_prices, sell_prices = _numbers(row.get("buy_prices", [])), _numbers(row.get("sell_prices", []))
        if not buys or not sells or not buy_prices or not sell_prices:
            continue
        if buy_prices[0] <= 0 or sell_prices[0] <= 0:
            continue
        stocks.setdefault(str(row["stock_id"]), {"ticks": [], "books": []})["books"].append({
            "time": _parse_stamp(row["received_at"]),
            "buy_volume": sum(buys),
            "sell_volume": sum(sells),
            "best_bid": buy_prices[0],
            "best_ask": sell_prices[0],
        })
    for symbol, data in stocks.items():
        data["ticks"].sort(key=lambda row: (row["time"], row["serial"]))
        data["books"].sort(key=lambda row: row["time"])
        data["tick_times"] = [row["time"] for row in data["ticks"]]
        data["book_times"] = [row["time"] for row in data["books"]]
        data["meta"] = {"stock_name": metadata.get(symbol, symbol)}
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    data = stocks.get(benchmark)
    if data is None or not data["ticks"] or not data["books"]:
        raise RuntimeError(f"paper replay requires complete {benchmark} context")
    return stocks


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _signed_volume(row: dict[str, Any]) -> float:
    if row.get("flag") == "1":
        return float(row["volume"])
    if row.get("flag") == "0":
        return -float(row["volume"])
    midpoint = (float(row["bid"]) + float(row["ask"])) / 2
    return float(row["volume"]) if float(row["price"]) >= midpoint else -float(row["volume"])


def _percentile90(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[index]


def _confirm_after_60_seconds(
    candidate: Any,
    data: dict[str, Any],
    *,
    capital_twd: int,
) -> tuple[Any | None, dict[str, Any]]:
    """Causal paper-only delayed confirmation; never reaches a broker adapter."""
    signal_time = candidate.decision_time
    checkpoint = signal_time + timedelta(
        seconds=int(PAPER_CONFIRMATION_POLICY["delay_seconds"])
    )
    observations = [row for row in data["ticks"] if row["time"] <= checkpoint]
    observation = observations[-1] if observations else None
    execution = next((row for row in data["ticks"] if row["time"] >= checkpoint), None)
    if observation is None or execution is None:
        return None, {"confirmation_time": checkpoint, "confirmation_reason": "QUOTE_MISSING"}
    maximum_age = float(SPEC["maximum_tick_staleness_seconds"])
    observation_age = (checkpoint - observation["time"]).total_seconds()
    execution_delay = (execution["time"] - checkpoint).total_seconds()
    if not (
        0 <= observation_age <= maximum_age
        and 0 <= execution_delay <= maximum_age
    ):
        return None, {
            "confirmation_time": checkpoint,
            "confirmation_reason": "QUOTE_STALE",
            "observation_age_seconds": observation_age,
            "execution_delay_seconds": execution_delay,
        }
    window = [
        row for row in data["ticks"]
        if signal_time < row["time"] <= checkpoint
    ]
    total_volume = sum(float(row["volume"]) for row in window)
    if not window or total_volume <= 0:
        return None, {"confirmation_time": checkpoint, "confirmation_reason": "FLOW_WINDOW_MISSING"}
    direction = 1.0 if candidate.side == "LONG" else -1.0
    volume_delta = direction * sum(_signed_volume(row) for row in window) / total_volume
    reference = [
        row for row in data["ticks"]
        if checkpoint - timedelta(seconds=300) <= row["time"] <= checkpoint
    ]
    if not reference:
        return None, {"confirmation_time": checkpoint, "confirmation_reason": "REFERENCE_MISSING"}
    threshold = _percentile90([float(row["volume"]) for row in reference])
    large = [row for row in window if float(row["volume"]) >= threshold]
    large_total = sum(float(row["volume"]) for row in large)
    large_delta = (
        direction * sum(_signed_volume(row) for row in large) / large_total
        if large_total > 0 else None
    )
    current = float(observation["price"])
    boundary = float(candidate.breakout_boundary_price)
    structure_held = current >= boundary if candidate.side == "LONG" else current <= boundary
    causal = observations
    known_volume = sum(float(row["volume"]) for row in causal)
    vwap = (
        sum(float(row["price"]) * float(row["volume"]) for row in causal) / known_volume
        if known_volume > 0 else None
    )
    opening = float(causal[0]["price"])
    if candidate.side == "LONG":
        opening_extension = current / opening - 1
        vwap_extension = current / float(vwap) - 1 if vwap else None
    else:
        opening_extension = opening / current - 1
        vwap_extension = float(vwap) / current - 1 if vwap else None
    checks = {
        "VOLUME_NOT_PERSISTENT": volume_delta >= float(
            PAPER_CONFIRMATION_POLICY["minimum_directional_volume_delta"]
        ),
        "LARGE_TRADE_NOT_PERSISTENT": large_delta is not None and large_delta >= float(
            PAPER_CONFIRMATION_POLICY["minimum_directional_large_trade_delta"]
        ),
        "BREAKOUT_NOT_HELD": structure_held,
        "OPENING_EXTENSION_TOO_HIGH": opening_extension <= float(
            ANTI_CHASE_ENTRY_POLICY["maximum_directional_opening_extension"]
        ) + 1e-12,
        "VWAP_EXTENSION_TOO_HIGH": vwap_extension is not None and vwap_extension <= float(
            ANTI_CHASE_ENTRY_POLICY["maximum_directional_vwap_extension"]
        ) + 1e-12,
    }
    failed = [reason for reason, passed in checks.items() if not passed]
    diagnostics = {
        "confirmation_time": checkpoint,
        "confirmation_volume_delta": volume_delta,
        "confirmation_large_trade_delta": large_delta,
        "confirmation_structure_held": structure_held,
        "confirmation_opening_extension": opening_extension,
        "confirmation_vwap_extension": vwap_extension,
        "observation_age_seconds": observation_age,
        "execution_delay_seconds": execution_delay,
    }
    if failed:
        return None, {**diagnostics, "confirmation_reason": "+".join(failed)}
    entry_price = (
        float(execution["ask"]) + _tick_size(float(execution["ask"]))
        if candidate.side == "LONG"
        else max(
            _tick_size(float(execution["bid"])),
            float(execution["bid"]) - _tick_size(float(execution["bid"])),
        )
    )
    quantity = math.floor(capital_twd / (entry_price * 1000)) * 1000
    if quantity <= 0:
        return None, {**diagnostics, "confirmation_reason": "DELAYED_ENTRY_UNAFFORDABLE"}
    confirmed = replace(
        candidate,
        decision_time=execution["time"],
        entry_price=entry_price,
        quantity=quantity,
        directional_opening_extension=opening_extension,
        directional_vwap_extension=float(vwap_extension),
    )
    return confirmed, {
        **diagnostics,
        "confirmation_reason": "CONFIRMED",
        "original_signal_time": signal_time,
        "delayed_entry_time": execution["time"],
        "delayed_entry_price": entry_price,
        "delayed_quantity": quantity,
    }


def _research_trade(candidate: Any, data: dict, end_time: datetime) -> ResearchTrade:
    points = []
    for row in data["ticks"]:
        if not candidate.decision_time < row["time"] <= end_time:
            continue
        exit_price = _exit_quote("LONG", row)
        projected = float(
            _projected_net_pnl("LONG", candidate.entry_price, exit_price, candidate.quantity)[3]
        )
        points.append(PathPoint(
            at=row["time"],
            exit_price=exit_price,
            projected_net_pnl=projected,
            current_return=(projected / (candidate.entry_price * candidate.quantity)),
            reversal=False,
        ))
    stop = derive_initial_stop_price(
        "LONG", candidate.entry_price, candidate.quantity,
        float(LIVE_EXIT_POLICY["stop_loss_net_twd"]),
    )
    return ResearchTrade(
        trade_id=(
            f"{candidate.decision_time.strftime('%Y%m%d')}-{candidate.stock_id}-"
            f"{candidate.decision_time.strftime('%H%M%S')}"
        ),
        session_date=candidate.decision_time.strftime("%Y%m%d"),
        symbol=candidate.stock_id,
        stock_name=candidate.stock_name,
        side="LONG",
        entry_time=candidate.decision_time,
        entry_price=candidate.entry_price,
        quantity=candidate.quantity,
        initial_stop_price=stop,
        points=tuple(points),
        force_last_point_exit=True,
    )


def _buffered_shadow_trades(
    production_trade: dict[str, Any], research: ResearchTrade,
) -> dict[str, dict[str, Any]]:
    existing = ExistingExit(
        at=_parse_stamp(production_trade["exit_time"]),
        price=float(production_trade["exit_price"]),
        net_pnl=float(production_trade["net_pnl"]),
        reason=str(production_trade["exit_reason"]),
    )
    output = {}
    for policy in BUFFERED_EXIT_POLICIES:
        result = simulate_buffered_exit(
            research, policy, existing_exit=existing,
        )
        gross, commission, sell_tax, net_pnl = _projected_net_pnl(
            "LONG", research.entry_price, result.exit_price, research.quantity,
        )
        trade = {
            **production_trade,
            "paper_trade_id": f"{production_trade['paper_trade_id']}-{policy.name}",
            "strategy_variant": policy.name,
            "exit_time": result.exit_time.isoformat(),
            "exit_price": result.exit_price,
            "exit_reason": result.exit_reason,
            "gross_pnl": gross,
            "commission": commission,
            "sell_tax": sell_tax,
            "net_pnl": net_pnl,
            "holding_seconds": result.holding_seconds,
            "source_entry_variant": "PRODUCTION_ANTI_CHASE",
            "original_exit_time": production_trade["exit_time"],
            "original_exit_price": production_trade["exit_price"],
            "original_exit_reason": production_trade["exit_reason"],
            "original_net_pnl": production_trade["net_pnl"],
            "buffered_exit_policy": asdict(policy),
            **buffered_result_dict(result),
        }
        # Recomputed costs are authoritative for the selected observed quote.
        trade["net_pnl"] = net_pnl
        output[policy.name] = trade
    return output


def _replay_paper_track(
    candidates: dict[str, dict],
    market_context: dict[str, dict],
    coverage: dict,
    *,
    capital_twd: int = 190_000,
    confirmation_60s: bool = False,
    engine_factory: Any = LiveDirectionEngine,
) -> dict[str, Any]:
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    combined = {**candidates, **market_context}
    metadata = {
        symbol: str(data.get("meta", {}).get("stock_name", symbol))
        for symbol, data in combined.items()
    }
    engine = engine_factory(
        metadata,
        capital_twd=capital_twd,
        candidate_symbols=set(candidates),
        benchmark_symbol=benchmark,
    )
    tick_indexes = {symbol: 0 for symbol in combined}
    book_indexes = {symbol: 0 for symbol in combined}
    session_date = str(coverage["session_date"])
    decisions = []
    candidate = None
    signal_time = None
    next_decision_after = None
    for decision in _decision_times(session_date, SPEC["entry_start"], SPEC["last_entry_time"]):
        if next_decision_after is not None and decision <= next_decision_after:
            continue
        _feed_until(engine, combined, tick_indexes, book_indexes, decision)
        candidate = engine.choose_entry(decision, allow_short=False)
        decisions.append(_jsonable(engine.last_entry_diagnostics))
        if candidate is not None:
            signal_time = candidate.decision_time
            if not confirmation_60s:
                break
            confirmed, confirmation = _confirm_after_60_seconds(
                candidate,
                candidates[candidate.stock_id],
                capital_twd=capital_twd,
            )
            decisions.append({
                "decision": "PAPER_60S_CONFIRMATION",
                "stock_id": candidate.stock_id,
                **_jsonable(confirmation),
            })
            checkpoint = signal_time + timedelta(
                seconds=int(PAPER_CONFIRMATION_POLICY["delay_seconds"])
            )
            if confirmed is None:
                _feed_until(engine, combined, tick_indexes, book_indexes, checkpoint)
                next_decision_after = checkpoint
                candidate = None
                signal_time = None
                continue
            candidate = confirmed
            _feed_until(
                engine, combined, tick_indexes, book_indexes,
                candidate.decision_time,
            )
            break
    if candidate is None:
        return {
            "trade": None,
            "checkpoint_rows": [],
            "candidate_impacts": [],
            "candidate_grid": [],
            "decision_diagnostics": decisions,
            "buffered_trades": {},
            "reason": (
                "NO_CONFIRMED_LONG_ENTRY"
                if confirmation_60s else "NO_APPROVED_LONG_ENTRY"
            ),
        }
    position = ManagedPosition(
        stock_id=candidate.stock_id,
        stock_name=candidate.stock_name,
        side="LONG",
        quantity=candidate.quantity,
        entry_price=candidate.entry_price,
        entry_order_id="PAPER_ONLY_NO_BROKER_ORDER",
        entry_time=candidate.decision_time,
    )
    data = candidates[candidate.stock_id]
    events: list[tuple[datetime, int, str, dict | None]] = []
    for row in data["ticks"][tick_indexes[candidate.stock_id]:]:
        events.append((row["time"], 0, "tick", row))
    for row in data["books"][book_indexes[candidate.stock_id]:]:
        events.append((row["time"], 1, "book", row))
    end_time = _parse_stamp(coverage["ended_at_taipei"])
    decision = candidate.decision_time + timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    while decision <= end_time:
        events.append((decision, 2, "decision", None))
        decision += timedelta(seconds=int(SPEC["decision_interval_seconds"]))
    events.sort(key=lambda item: (item[0], item[1]))
    exit_decision = None
    exit_time = None
    for at, _priority, kind, row in events:
        reversal = False
        if kind == "tick":
            assert row is not None
            _record_tick(engine, candidate.stock_id, row)
        elif kind == "book":
            assert row is not None
            _record_book(engine, candidate.stock_id, row)
        else:
            reversal = engine.opposite_signal(position, at)
            engine.last_decision = at
        exit_decision = engine.evaluate_exit(
            position, at, reversal=reversal, max_quote_age_seconds=3.0,
        )
        if exit_decision is not None:
            exit_time = at
            break
    if exit_decision is None or exit_time is None:
        return {
            "trade": None,
            "checkpoint_rows": [],
            "candidate_impacts": [],
            "candidate_grid": [],
            "decision_diagnostics": decisions,
            "buffered_trades": {},
            "reason": "EXIT_NOT_SCORABLE_FROM_FRESH_QUOTES",
            "selected_signal": _jsonable(asdict(candidate)),
        }
    gross, commission, sell_tax, net_pnl = _projected_net_pnl(
        "LONG", candidate.entry_price, exit_decision.price, candidate.quantity,
    )
    trade = {
        "paper_trade_id": (
            f"{session_date}-{candidate.stock_id}-{candidate.decision_time.strftime('%H%M%S')}"
        ),
        "session_date": session_date,
        "stock_id": candidate.stock_id,
        "stock_name": candidate.stock_name,
        "side": "LONG",
        "strategy_variant": (
            "ANTI_CHASE_PLUS_60S_CONFIRMATION"
            if confirmation_60s else "PRODUCTION_ANTI_CHASE"
        ),
        "signal_time": (signal_time or candidate.decision_time).isoformat(),
        "entry_time": candidate.decision_time.isoformat(),
        "entry_price": candidate.entry_price,
        "quantity": candidate.quantity,
        "notional_used": candidate.entry_price * candidate.quantity,
        "exit_time": exit_time.isoformat(),
        "exit_price": exit_decision.price,
        "exit_reason": exit_decision.reason,
        "gross_pnl": gross,
        "commission": commission,
        "sell_tax": sell_tax,
        "net_pnl": net_pnl,
        "holding_seconds": (exit_time - candidate.decision_time).total_seconds(),
        "score": candidate.score,
        "volume_delta": candidate.volume_delta,
        "large_trade_delta": candidate.large_trade_delta,
        "vwap_gap": candidate.vwap_gap,
        "book_imbalance": candidate.book_imbalance,
        "spread_bps": candidate.spread_bps,
        "market_regime": candidate.market_regime,
        "benchmark_vwap_gap": candidate.benchmark_vwap_gap,
        "benchmark_return_5m": candidate.benchmark_return_5m,
        "relative_strength_5m": candidate.relative_strength_5m,
        "required_confirmations": candidate.required_confirmations,
        "directional_opening_extension": candidate.directional_opening_extension,
        "directional_vwap_extension": candidate.directional_vwap_extension,
        "breakout_boundary_price": candidate.breakout_boundary_price,
        "execution_model": EXECUTION_MODEL,
        "paper_only": True,
    }
    research = _research_trade(candidate, data, end_time)
    outcome = Outcome(
        "SCORED", exit_time, exit_decision.price, exit_decision.reason,
        net_pnl, trade["holding_seconds"],
    )
    checkpoint_rows = build_checkpoint_rows(research, outcome)
    case = TradeCase(research, outcome, tuple(checkpoint_rows))
    impacts = _impact_rows([case])
    grid = _grid_rows(impacts)
    buffered_trades = (
        _buffered_shadow_trades(trade, research)
        if not confirmation_60s else {}
    )
    return {
        "trade": trade,
        "checkpoint_rows": checkpoint_rows,
        "candidate_impacts": impacts,
        "candidate_grid": grid,
        "decision_diagnostics": decisions,
        "buffered_trades": buffered_trades,
        "reason": "PAPER_TRADE_SCORED",
        "selected_signal": _jsonable(asdict(candidate)),
    }


def replay_paper_session(
    candidates: dict[str, dict],
    market_context: dict[str, dict],
    coverage: dict,
    *,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    _validate_full_session_coverage(coverage)
    benchmark = str(LONG_MARKET_REGIME_POLICY["benchmark_symbol"])
    combined = {**candidates, **market_context}
    if benchmark not in combined:
        raise RuntimeError("benchmark is missing from paper replay")
    anti_chase = _replay_paper_track(
        candidates, market_context, coverage,
        capital_twd=capital_twd,
        confirmation_60s=False,
    )
    confirmed = _replay_paper_track(
        candidates, market_context, coverage,
        capital_twd=capital_twd,
        confirmation_60s=True,
    )
    buffered_trades = anti_chase.get("buffered_trades", {})
    return {
        **anti_chase,
        "variants": {
            "PRODUCTION_ANTI_CHASE": anti_chase,
            "ANTI_CHASE_PLUS_60S_CONFIRMATION": confirmed,
            **{
                name: {
                    "trade": trade,
                    "reason": "PAPER_TRADE_SCORED",
                    "source_entry_variant": "PRODUCTION_ANTI_CHASE",
                }
                for name, trade in buffered_trades.items()
            },
        },
        "buffered_trades": buffered_trades,
        "confirmation_trade": confirmed["trade"],
        "confirmation_reason": confirmed["reason"],
        "confirmation_decision_diagnostics": confirmed["decision_diagnostics"],
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _verify_published(path: Path) -> dict[str, Any]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError("paper manifest hash mismatch")
    for name, digest in manifest["artifacts"].items():
        if sha256_file(path / name) != digest:
            raise RuntimeError(f"paper artifact hash mismatch: {name}")
    return manifest


def publish_paper_day(
    run_dir: Path,
    session_manifest: dict[str, Any],
    *,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    capital_twd: int = 190_000,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    if session_manifest.get("coverage_status") != "FULL_SESSION":
        raise RuntimeError("paper shadow requires FULL_SESSION coverage")
    source_unsigned = {key: value for key, value in source.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(source_unsigned)).hexdigest() != source.get("manifest_hash"):
        raise RuntimeError("paper shadow source manifest hash mismatch")
    session_unsigned = {
        key: value for key, value in session_manifest.items() if key != "analysis_hash"
    }
    if hashlib.sha256(canonical_bytes(session_unsigned)).hexdigest() != session_manifest.get("analysis_hash"):
        raise RuntimeError("paper shadow session analysis hash mismatch")
    if not bool(session_manifest.get("stream_coverage_pass")):
        raise RuntimeError("paper shadow requires stream coverage pass")
    for field in ("actual_orders", "actual_fills", "broker_order_calls"):
        if int(session_manifest.get(field, 0)) != 0:
            raise RuntimeError(f"paper shadow rejects nonzero {field}")
    if session_manifest.get("source_run_id") != source.get("run_id"):
        raise RuntimeError("paper shadow source run mismatch")
    if session_manifest.get("source_manifest_hash") != source.get("manifest_hash"):
        raise RuntimeError("paper shadow source manifest mismatch")
    identity = {
        "source_manifest_hash": source["manifest_hash"],
        "session_analysis_hash": session_manifest["analysis_hash"],
        "paper_contract_hash": PAPER_CONTRACT_HASH,
    }
    paper_run_id = hashlib.sha256(canonical_bytes(identity)).hexdigest()[:24]
    day = str(session_manifest["session_date"])
    target = runtime_dir / "days" / day / paper_run_id
    if target.is_dir():
        manifest = _verify_published(target)
        return {**manifest, "publish_status": "ALREADY_PUBLISHED", "run_dir": str(target)}
    candidates, coverage = load_session([run_dir])
    _validate_full_session_coverage(coverage)
    market_context = _load_market_context(run_dir, source)
    result = replay_paper_session(
        candidates, market_context, coverage, capital_twd=capital_twd,
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{paper_run_id}.tmp-{os.getpid()}"
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        buffered_trades = result.get("buffered_trades", {})
        trade_rows = [
            trade for trade in (
                result["trade"], result["confirmation_trade"],
                *buffered_trades.values(),
            )
            if trade is not None
        ]
        _write_jsonl(temporary / "paper_trades.jsonl", trade_rows)
        _write_jsonl(temporary / "decision_diagnostics.jsonl", result["decision_diagnostics"])
        _write_jsonl(
            temporary / "confirmation_60s_diagnostics.jsonl",
            result["confirmation_decision_diagnostics"],
        )
        _write_jsonl(temporary / "early_failure_checkpoints.jsonl", result["checkpoint_rows"])
        _write_jsonl(temporary / "early_failure_candidate_impacts.jsonl", result["candidate_impacts"])
        _write_jsonl(temporary / "early_failure_grid.jsonl", result["candidate_grid"])
        summary = {
            "analysis_id": ANALYSIS_ID,
            "paper_run_id": paper_run_id,
            "session_date": day,
            "source_run_id": source["run_id"],
            "paper_contract_hash": PAPER_CONTRACT_HASH,
            "status": "PAPER_VARIANTS_EVALUATED",
            "paper_trade_count": len(trade_rows),
            "net_pnl": result["trade"]["net_pnl"] if result["trade"] else 0,
            "net_pnl_scope": "PRODUCTION_ANTI_CHASE_ONLY_NOT_VARIANT_SUM",
            "variant_results": {
                "PRODUCTION_ANTI_CHASE": {
                    "status": result["reason"],
                    "trade_count": int(result["trade"] is not None),
                    "net_pnl": result["trade"]["net_pnl"] if result["trade"] else 0,
                },
                "ANTI_CHASE_PLUS_60S_CONFIRMATION": {
                    "status": result["confirmation_reason"],
                    "trade_count": int(result["confirmation_trade"] is not None),
                    "net_pnl": (
                        result["confirmation_trade"]["net_pnl"]
                        if result["confirmation_trade"] else 0
                    ),
                },
                **{
                    policy.name: {
                        "status": (
                            "PAPER_TRADE_SCORED"
                            if policy.name in buffered_trades
                            else "NO_APPROVED_LONG_ENTRY"
                        ),
                        "trade_count": int(policy.name in buffered_trades),
                        "net_pnl": (
                            buffered_trades[policy.name]["net_pnl"]
                            if policy.name in buffered_trades else 0
                        ),
                    }
                    for policy in BUFFERED_EXIT_POLICIES
                },
            },
            "checkpoint_rows": len(result["checkpoint_rows"]),
            "evaluable_checkpoints": sum(bool(row["evaluable"]) for row in result["checkpoint_rows"]),
            "candidate_impact_rows": len(result["candidate_impacts"]),
            "decision_windows": len(result["decision_diagnostics"]),
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
            "production_entry_policy": "ANTI_CHASE_BALANCED_V1",
            "production_entry_behavior_changed": True,
            "broker_submission_behavior_changed": False,
        }
        (temporary / "daily_summary.json").write_bytes(canonical_bytes(summary) + b"\n")
        artifact_names = (
            "paper_trades.jsonl", "decision_diagnostics.jsonl",
            "confirmation_60s_diagnostics.jsonl",
            "early_failure_checkpoints.jsonl",
            "early_failure_candidate_impacts.jsonl", "early_failure_grid.jsonl",
            "daily_summary.json",
        )
        manifest = {
            **summary,
            "source_manifest_hash": source["manifest_hash"],
            "session_analysis_hash": session_manifest["analysis_hash"],
            "artifacts": {
                name: sha256_file(temporary / name) for name in artifact_names
            },
        }
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (temporary / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    verified = _verify_published(target)
    return {**verified, "publish_status": "PUBLISHED", "run_dir": str(target)}
