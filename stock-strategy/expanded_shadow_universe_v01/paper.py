"""Immutable post-session paper replay for the expanded quote streams."""

from __future__ import annotations

from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Iterable

from paper_shadow_v01.runner import _load_market_context, replay_paper_session
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file
from yuanta_intraday_shadow_v01.direction_follow_backtest import _archived_exchange_time
from yuanta_intraday_shadow_v01.live_parity_backtest import _parse_stamp


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_RUNTIME_DIR = MODULE_DIR / "runtime"
PAPER_CONTRACT = {
    "analysis_id": "EXPANDED_SHADOW_POST_SESSION_PAPER_V1",
    "signal_engine": "PRODUCTION_LIVE_DIRECTION_ENGINE",
    "direction": "LONG_ONLY",
    "capital_twd": 190_000,
    "maximum_concurrent_positions": 1,
    "maximum_trades_per_session": 1,
    "source": "YUANTA_PROD_READ_ONLY_QUOTES",
    "execution": "POST_SESSION_CAUSAL_PAPER_ONLY",
    "actual_orders": 0,
    "actual_fills": 0,
    "broker_order_calls": 0,
}
PAPER_CONTRACT_HASH = hashlib.sha256(canonical_bytes(PAPER_CONTRACT)).hexdigest()


def _read_jsonl(path: Path) -> Iterable[dict]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _numbers(values) -> list[float]:
    output = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            output.append(number)
    return output


def load_expanded_candidates(run_dir: Path) -> tuple[dict[str, dict], dict]:
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if hashlib.sha256(canonical_bytes(unsigned)).hexdigest() != manifest.get("manifest_hash"):
        raise RuntimeError("source run manifest hash mismatch")
    tick_name = next((name for name in (
        "expanded_ticks.jsonl.gz", "expanded_ticks.jsonl",
    ) if name in manifest.get("artifacts", {})), None)
    book_name = next((name for name in (
        "expanded_books.jsonl.gz", "expanded_books.jsonl",
    ) if name in manifest.get("artifacts", {})), None)
    if tick_name is None or book_name is None:
        raise RuntimeError("expanded quote artifacts are absent")
    for name in (tick_name, book_name, "watchlist.json"):
        if sha256_file(run_dir / name) != manifest["artifacts"][name]:
            raise RuntimeError(f"expanded source artifact hash mismatch: {name}")
    watchlist = json.loads((run_dir / "watchlist.json").read_text(encoding="utf-8"))
    expanded = watchlist.get("expanded_shadow_universe")
    if not isinstance(expanded, dict):
        raise RuntimeError("expanded universe snapshot is absent")
    if expanded.get("seal_hash") != manifest.get("expanded_universe_seal_hash"):
        raise RuntimeError("expanded universe seal identity mismatch")
    metadata = {str(row["stock_id"]): row for row in expanded.get("stocks", [])}
    if len(metadata) != int(manifest.get("expanded_universe_count", -1)):
        raise RuntimeError("expanded universe count mismatch")
    stocks: dict[str, dict] = {}
    for row in _read_jsonl(run_dir / tick_name):
        symbol = str(row.get("stock_id", ""))
        if symbol not in metadata:
            raise RuntimeError(f"expanded tick outside sealed universe: {symbol}")
        try:
            received_at = _parse_stamp(row["received_at"])
            item = {
                "time": received_at,
                "received_at": received_at,
                "exchange_time": _archived_exchange_time(row.get("quote_time"), received_at),
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
        stocks.setdefault(symbol, {"ticks": [], "books": []})["ticks"].append(item)
    for row in _read_jsonl(run_dir / book_name):
        symbol = str(row.get("stock_id", ""))
        if symbol not in metadata:
            raise RuntimeError(f"expanded book outside sealed universe: {symbol}")
        buys, sells = _numbers(row.get("buy_volumes")), _numbers(row.get("sell_volumes"))
        buy_prices, sell_prices = _numbers(row.get("buy_prices")), _numbers(row.get("sell_prices"))
        if not buys or not sells or not buy_prices or not sell_prices:
            continue
        if buy_prices[0] <= 0 or sell_prices[0] <= 0:
            continue
        stocks.setdefault(symbol, {"ticks": [], "books": []})["books"].append({
            "time": _parse_stamp(row["received_at"]),
            "buy_volume": sum(buys), "sell_volume": sum(sells),
            "best_bid": buy_prices[0], "best_ask": sell_prices[0],
        })
    for symbol, data in stocks.items():
        data["ticks"].sort(key=lambda row: (row["time"], row["serial"]))
        data["books"].sort(key=lambda row: row["time"])
        data["tick_times"] = [row["time"] for row in data["ticks"]]
        data["book_times"] = [row["time"] for row in data["books"]]
        data["meta"] = metadata[symbol]
    coverage = {
        "sealed_symbol_count": len(metadata),
        "symbols_with_ticks": sum(bool(data["ticks"]) for data in stocks.values()),
        "symbols_with_books": sum(bool(data["books"]) for data in stocks.values()),
        "symbols_with_both": sum(bool(data["ticks"] and data["books"]) for data in stocks.values()),
    }
    return stocks, coverage


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def publish_expanded_paper(
    run_dir: Path,
    session_manifest: dict,
    *,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    capital_twd: int = 190_000,
) -> dict:
    run_dir = run_dir.resolve()
    source = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    if session_manifest.get("coverage_status") != "FULL_SESSION":
        raise RuntimeError("expanded paper requires FULL_SESSION source coverage")
    for field in ("actual_orders", "actual_fills", "broker_order_calls"):
        if int(source.get(field, 0)) != 0 or int(session_manifest.get(field, 0)) != 0:
            raise RuntimeError(f"expanded paper rejects nonzero {field}")
    candidates, expanded_coverage = load_expanded_candidates(run_dir)
    market_context = _load_market_context(run_dir, source)
    coverage = {
        "session_date": str(session_manifest["session_date"]),
        "source_run_ids": [source["run_id"]],
        "source_statuses": [source["status"]],
        "started_at_taipei": source["started_at"],
        "ended_at_taipei": source["ended_at"],
        "callback_errors": int(source.get("event_counts", {}).get("callback_errors", 0)),
        "coverage_status": "FULL_SESSION",
    }
    result = replay_paper_session(candidates, market_context, coverage, capital_twd=capital_twd)
    identity = {
        "source_manifest_hash": source["manifest_hash"],
        "session_analysis_hash": session_manifest["analysis_hash"],
        "expanded_universe_seal_hash": source["expanded_universe_seal_hash"],
        "paper_contract_hash": PAPER_CONTRACT_HASH,
    }
    paper_run_id = hashlib.sha256(canonical_bytes(identity)).hexdigest()[:24]
    day = str(session_manifest["session_date"])
    target = runtime_dir / "days" / day / paper_run_id
    if target.exists():
        return json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    temporary = target.parent / f".{paper_run_id}.tmp-{os.getpid()}"
    temporary.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir()
    try:
        trades = [trade for trade in (
            result.get("trade"), result.get("confirmation_trade"),
        ) if trade is not None]
        (temporary / "paper_trades.jsonl").write_text(
            "".join(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n" for row in trades),
            encoding="utf-8",
        )
        (temporary / "decision_diagnostics.jsonl").write_text(
            "".join(json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n" for row in result.get("decision_diagnostics", [])),
            encoding="utf-8",
        )
        summary = {
            "analysis_id": PAPER_CONTRACT["analysis_id"],
            "paper_run_id": paper_run_id,
            "session_date": day,
            "source_run_id": source["run_id"],
            "expanded_universe_seal_hash": source["expanded_universe_seal_hash"],
            "expanded_coverage": expanded_coverage,
            "status": "EXPANDED_PAPER_EVALUATED",
            "production_strategy_reason": result["reason"],
            "production_strategy_trade_count": int(result.get("trade") is not None),
            "production_strategy_net_pnl": result["trade"]["net_pnl"] if result.get("trade") else 0,
            "confirmation_strategy_reason": result["confirmation_reason"],
            "confirmation_strategy_trade_count": int(result.get("confirmation_trade") is not None),
            "confirmation_strategy_net_pnl": result["confirmation_trade"]["net_pnl"] if result.get("confirmation_trade") else 0,
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0,
        }
        (temporary / "summary.json").write_bytes(canonical_bytes(summary) + b"\n")
        artifacts = {
            name: sha256_file(temporary / name)
            for name in ("paper_trades.jsonl", "decision_diagnostics.jsonl", "summary.json")
        }
        manifest = {**summary, **identity, "paper_contract": PAPER_CONTRACT, "artifacts": artifacts}
        manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        (temporary / "manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return json.loads((target / "manifest.json").read_text(encoding="utf-8"))
