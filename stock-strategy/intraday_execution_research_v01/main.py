from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from .aggregator import aggregate_ticks, validate_no_future_bar
from .config import CFG
from .features import FEATURE_NAMES, build_daily_snapshots
from .mock_feed import MockQuoteAdapter
from .recorder import TickRecorder
from .replay import load_ticks, replay
from .storage import immutable_json, sha256_file
from .validation import seal_day, validate_seal
from .watchlist import export_watchlist, verify_watchlist


def _json_artifact(path: Path, payload: dict) -> None:
    immutable_json(path, payload)


def healthcheck() -> dict:
    adapter = MockQuoteAdapter("2000-01-03")
    return {
        **adapter.healthcheck(),
        "formal_research_run_count": 0,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }


def prepare_watchlist(stock_strategy: Path, runtime: Path, signal_date: str) -> dict:
    path, payload = export_watchlist(stock_strategy, runtime, signal_date)
    verify_watchlist(payload)
    return {"status": "PREPARED", "path": str(path), "symbol_count": len(payload["symbols"]), **payload}


def run_mock_session(runtime: Path, signal_date: str) -> dict:
    watchlist_path = runtime / "watchlists" / f"{signal_date.replace('-', '')}.json"
    if not watchlist_path.exists():
        raise FileNotFoundError("prepare-watchlist must run first")
    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
    verify_watchlist(watchlist)
    trading_date = watchlist["subscription_trading_date_formatted"]
    symbols = [item["stock_code"] for item in watchlist["symbols"]]
    seal_path = runtime / "intraday_seals" / f"{trading_date.replace('-', '')}.json"
    if seal_path.exists():
        audit = validate_seal(runtime, seal_path)
        if not audit["pass"]:
            raise RuntimeError(f"existing immutable seal failed: {audit['errors']}")
        return {"status": "SEALED_EXISTING", "seal": str(seal_path), "seal_validation": audit}

    recorder = TickRecorder(runtime, trading_date, symbols, watchlist["watchlist_sha256"])
    recorder.prepare()
    recorder.start()
    adapter = MockQuoteAdapter(trading_date)
    adapter.connect()
    adapter.subscribe(symbols)
    events = list(adapter.events())
    midpoint = len(events) // 2
    for index, tick in enumerate(events):
        if index == midpoint:
            recorder.interrupt()
            adapter.simulate_reconnect()
            adapter.subscribe(symbols)
            recorder.start()
        recorder.record(tick)
    adapter.disconnect()

    raw_manifest = recorder.raw_manifest()
    raw_manifest.update({
        "adapter": "MOCK_ONLY", "reconnect_count": adapter.reconnect_count,
        "temporary_silence_simulated": True, "trading_halt_like_pause_simulated": True,
        "burst_volume_simulated": True,
    })
    raw_manifest_path = runtime / "intraday_manifests" / f"{trading_date.replace('-', '')}.json"
    _json_artifact(raw_manifest_path, raw_manifest)

    ticks = load_ticks(recorder.raw_dir)
    one = aggregate_ticks(ticks, 1)
    five = aggregate_ticks(ticks, 5)
    validate_no_future_bar(ticks, one, 1)
    validate_no_future_bar(ticks, five, 5)
    bar_dir = runtime / "intraday_bars" / trading_date.replace("-", "")
    one_path, five_path = bar_dir / "bars_1m.json", bar_dir / "bars_5m.json"
    _json_artifact(one_path, {"trading_date": trading_date, "bar_minutes": 1, "bars": one})
    _json_artifact(five_path, {"trading_date": trading_date, "bar_minutes": 5, "bars": five})

    by_symbol = {symbol: [] for symbol in symbols}
    for tick in ticks:
        by_symbol[tick.stock_code].append(tick)
    snapshots = build_daily_snapshots(by_symbol, watchlist)
    snapshot_path = runtime / "feature_snapshots" / f"{trading_date.replace('-', '')}.json"
    _json_artifact(snapshot_path, {
        "trading_date": trading_date, "snapshot_times": list(CFG.snapshot_times),
        "feature_names": list(FEATURE_NAMES), "rows": snapshots,
    })
    seal_path, seal = seal_day(
        runtime, trading_date, watchlist["watchlist_sha256"], raw_manifest,
        one_path, five_path, snapshot_path,
    )
    recorder.mark_sealed()
    audit = validate_seal(runtime, seal_path)
    if not audit["pass"]:
        raise RuntimeError(f"new daily seal failed: {audit['errors']}")
    return {
        "status": "SEALED", "signal_date": signal_date, "trading_date": trading_date,
        "symbols": len(symbols), "ticks_written": raw_manifest["tick_count"],
        "duplicates_suppressed": raw_manifest["duplicate_count"],
        "out_of_order_events": raw_manifest["out_of_order_count"],
        "one_minute_bars": len(one), "five_minute_bars": len(five),
        "feature_snapshots": len(snapshots), "seal": str(seal_path),
        "seal_sha256": sha256_file(seal_path), "seal_validation": audit,
        "formal_research_run_count": 0,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }


def publish_infrastructure(package: Path, runtime: Path, signal_date: str) -> dict:
    validation_path = package / "validation_summary.json"
    manifest_path = package / "run_manifest.json"
    if validation_path.exists() or manifest_path.exists():
        raise FileExistsError("refusing to overwrite published infrastructure validation")
    watchlist = json.loads((runtime / "watchlists" / f"{signal_date.replace('-', '')}.json").read_text(encoding="utf-8"))
    verify_watchlist(watchlist)
    trading_date = watchlist["subscription_trading_date_formatted"]
    seal_path = runtime / "intraday_seals" / f"{trading_date.replace('-', '')}.json"
    seal_audit = validate_seal(runtime, seal_path)
    if not seal_audit["pass"]:
        raise RuntimeError("mock daily seal is not valid")
    validation = {
        "study_id": CFG.study_id,
        "classification": CFG.classification,
        "mock_session_date": trading_date,
        "watchlist_symbols": 30,
        "snapshot_times": list(CFG.snapshot_times),
        "formal_research_run_count": 0,
        "model_fit_count": 0,
        "stage_a_refit_count": 0,
        "real_intraday_observations": 0,
        "mock_daily_seal_validation": seal_audit,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    manifest = {
        "status": "COMPLETE", "study_id": CFG.study_id,
        "classification": CFG.classification, "config_hash": CFG.fingerprint(),
        "frozen_stage_a": watchlist["frozen_model"],
        "mock_artifacts": {"watchlist_sha256": watchlist["watchlist_sha256"], "seal_sha256": sha256_file(seal_path)},
        "pipeline": {
            "adapter": "MOCK_ONLY", "append_only_raw": True,
            "deterministic_1m_5m_aggregation": True, "snapshot_no_future_data": True,
            "entry_proxy_strictly_after_snapshot": True, "sealed_day_immutable": True,
        },
        "formal_research_run_count": 0, "model_fit_count": 0,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    immutable_json(validation_path, validation)
    immutable_json(manifest_path, manifest)
    return validation


def main(argv: list[str] | None = None) -> int:
    package = Path(__file__).resolve().parent
    stock_strategy = package.parent
    runtime = package / "runtime"
    parser = argparse.ArgumentParser(description="Mock-only intraday execution research infrastructure")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("healthcheck")
    prepare = sub.add_parser("prepare-watchlist")
    prepare.add_argument("--date", required=True)
    mock = sub.add_parser("run-mock-session")
    mock.add_argument("--date", required=True)
    replay_parser = sub.add_parser("replay")
    replay_parser.add_argument("--date", required=True)
    replay_parser.add_argument("--speed", choices=("instant", "accelerated", "realtime"), default="instant")
    publish = sub.add_parser("publish-infrastructure")
    publish.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    if args.command == "healthcheck": result = healthcheck()
    elif args.command == "prepare-watchlist": result = prepare_watchlist(stock_strategy, runtime, args.date)
    elif args.command == "run-mock-session": result = run_mock_session(runtime, args.date)
    elif args.command == "replay":
        raw = runtime / "intraday_raw" / args.date.replace("-", "")
        digests = []
        count = replay(raw, lambda tick: digests.append(tick.duplicate_key()), args.speed)
        result = {"status": "REPLAY_COMPLETE", "ticks": count, "deterministic_digest": __import__('hashlib').sha256(''.join(digests).encode()).hexdigest()}
    else: result = publish_infrastructure(package, runtime, args.date)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
