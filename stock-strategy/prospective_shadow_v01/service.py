from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from .config import CFG, Config
from .detector import assert_frozen_contract, scan_snapshot
from .market_data_provider import MarketDataProvider, normalize_date
from .outcomes import build_outcome_candidates
from .storage import ShadowStore


def _aware_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _after_close(now: datetime, cfg: Config) -> tuple[str, datetime]:
    local = now.astimezone(ZoneInfo(cfg.timezone))
    target = local.strftime("%Y%m%d")
    ready = time.fromisoformat(cfg.market_close_ready_time)
    if local.time().replace(tzinfo=None) < ready:
        raise RuntimeError(
            f"daily shadow scan is allowed only after {cfg.market_close_ready_time} {cfg.timezone}"
        )
    if target < cfg.prospective_start_date:
        raise RuntimeError("prospective collection has not started")
    return target, local


def update_outcomes_from_snapshot(
    store: ShadowStore,
    snapshot,
    as_of_date: str,
    *,
    now: datetime | None = None,
    cfg: Config = CFG,
) -> dict:
    signals = store.read_signals()
    outcomes = store.read_outcomes()
    latest: dict[str, dict[str, str]] = {}
    for row in outcomes:
        latest[row["signal_key"]] = row
    active = [
        row
        for row in signals
        if latest.get(row["signal_key"], {}).get("is_final") != "true"
    ]
    if not active:
        return {"status": "NO_ACTIVE_SIGNALS", "appended_outcomes": 0}
    candidates = build_outcome_candidates(snapshot, active, as_of_date, cfg)
    result = store.append_outcomes(candidates, now=now)
    return result


def run_daily(
    provider: MarketDataProvider,
    store: ShadowStore,
    *,
    now: datetime | None = None,
    cfg: Config = CFG,
) -> dict:
    moment = _aware_now(now)
    target, _ = _after_close(moment, cfg)
    assert_frozen_contract(cfg)
    snapshot = provider.load_through(target)
    scan = scan_snapshot(snapshot, target, cfg)
    scan_result = store.append_scan(
        scan,
        provider_name=snapshot.provider_name,
        input_manifest_hash=snapshot.input_manifest_hash,
        now=moment,
    )
    outcome_result = update_outcomes_from_snapshot(
        store, snapshot, target, now=moment, cfg=cfg
    )
    status = store.validate()
    return {
        "command": "run-daily",
        "signal_date": target,
        "scan": scan_result,
        "outcomes": outcome_result,
        "signal_count": scan.compact_count,
        "status": status,
        "provider_audit": snapshot.audit,
        "input_manifest_hash": snapshot.input_manifest_hash,
        "actual_orders": 0,
        "actual_fills": 0,
    }


def update_outcomes(
    provider: MarketDataProvider,
    store: ShadowStore,
    as_of_date: str,
    *,
    now: datetime | None = None,
    cfg: Config = CFG,
) -> dict:
    moment = _aware_now(now)
    as_of = normalize_date(as_of_date)
    today = moment.astimezone(ZoneInfo(cfg.timezone)).strftime("%Y%m%d")
    if as_of > today:
        raise RuntimeError("future outcome as-of dates are forbidden")
    if as_of == today:
        _after_close(moment, cfg)
    assert_frozen_contract(cfg)
    signals = store.read_signals()
    if not signals:
        return {
            "command": "update-outcomes",
            "as_of_date": as_of,
            "outcomes": {"status": "NO_SIGNALS", "appended_outcomes": 0},
            "actual_orders": 0,
            "actual_fills": 0,
        }
    snapshot = provider.load_through(as_of)
    result = update_outcomes_from_snapshot(
        store, snapshot, as_of, now=moment, cfg=cfg
    )
    return {
        "command": "update-outcomes",
        "as_of_date": as_of,
        "outcomes": result,
        "status": store.validate(),
        "provider_audit": snapshot.audit,
        "input_manifest_hash": snapshot.input_manifest_hash,
        "actual_orders": 0,
        "actual_fills": 0,
    }
