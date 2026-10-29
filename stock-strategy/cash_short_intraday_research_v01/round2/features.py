"""Receipt-causal features for the six predeclared round-two S1 comparisons.

This module does not select an S1 setup, size an order, compute performance, or
call a broker.  It annotates *existing* round-one S1 signals and verifies their
shared features against the same frozen reader/engine.  VWAP(t-180) is saved at
that historical receipt cursor, not reconstructed using data received later.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timedelta
import math

from ..signals import SignalScanner, _stamp


VARIANTS = ("R0", "R1", "R2", "R3", "R4", "R5")
RS300_MAXIMUM = -0.002
VWAP_HISTORY_SECONDS = 180
ENTRY_DISTANCE_MAXIMUM = 0.003


class FeatureMismatchError(ValueError):
    """Archived shared features do not match the supplied replay inputs."""


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _same(left, right):
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if _finite(left) and _finite(right):
        return math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-12)
    return left == right


def _grid(day, tz):
    start = datetime.strptime(day + "090500", "%Y%m%d%H%M%S").replace(tzinfo=tz)
    return [start + timedelta(seconds=30 * n) for n in range(491)]


def _market_snapshot(scanner, now):
    """Use the *original* S1_WEAK availability gate, not a stricter book gate.

    Original S1_WEAK depends on benchmark ticks and its inherited market engine,
    not benchmark five-level eligibility.  Enforcing a new book rule here would
    silently change R1.  The observed return endpoint times are exposed instead
    of pretending a sparse historical endpoint was exactly t-300.
    """
    base = {"time": now.isoformat(), "market_available": False,
            "long_market_evaluable": False, "unavailable_reasons": [],
            "benchmark_price": None, "benchmark_vwap": None,
            "benchmark_vwap_gap": None, "benchmark_return_5m": None,
            "market_filter_pass": None, "market_regime": "UNAVAILABLE",
            "benchmark_last_exchange_at": None, "benchmark_last_received_at": None,
            "benchmark_300s_reference_exchange_at": None,
            "benchmark_300s_reference_received_at": None,
            "benchmark_300s_reference_age_from_target_seconds": None,
            "basis": "ORIGINAL_S1_WEAK_TICK_GATE_NO_NEW_BOOK_REQUIREMENT"}
    if "0050" not in scanner.observed:
        base["unavailable_reasons"] = ["BENCHMARK_0050_NOT_IN_ARCHIVE"]
        return base
    reason = scanner._warm_reason("0050", now)
    if reason is not None:
        base["unavailable_reasons"] = ["BENCHMARK_" + reason]
        return base
    raw = scanner.engine._states["0050"]
    rows = scanner.engine._causal_rows(raw, now)
    if rows:
        base["benchmark_last_exchange_at"] = rows[-1]["time"].isoformat()
        base["benchmark_last_received_at"] = rows[-1]["received_at"].isoformat()
    cutoff = now - timedelta(seconds=300)
    reference = [row for row in rows if row["time"] <= cutoff]
    if reference:
        base["benchmark_300s_reference_exchange_at"] = reference[-1]["time"].isoformat()
        base["benchmark_300s_reference_received_at"] = reference[-1]["received_at"].isoformat()
        base["benchmark_300s_reference_age_from_target_seconds"] = (cutoff - reference[-1]["time"]).total_seconds()
    market = scanner.engine._market_context(now)
    if market is None:
        base["unavailable_reasons"] = ["BENCHMARK_CONTEXT_OR_300S_REFERENCE_UNAVAILABLE"]
        return base
    price, gap, change = market["benchmark_price"], market["benchmark_vwap_gap"], market["benchmark_return_5m"]
    if not all(_finite(value) for value in (price, gap, change)) or 1 + gap <= 0:
        base["unavailable_reasons"] = ["BENCHMARK_NONFINITE_FEATURE"]
        return base
    base.update(market_available=True, long_market_evaluable=True,
                benchmark_price=price, benchmark_vwap=price / (1 + gap),
                benchmark_vwap_gap=gap, benchmark_return_5m=change,
                market_filter_pass=gap < 0 and change <= 0,
                market_regime=market["market_regime"])
    return base


def _stock_snapshot(scanner, symbol, now):
    feature = scanner.features(symbol, now)
    if symbol not in scanner.observed:
        return {"features": feature, "historical_vwap_available": False,
                "historical_vwap_unavailable_reasons": ["UNKNOWN_SYMBOL"]}
    observed = scanner.observed[symbol]
    raw = scanner.engine._states[symbol]
    rows = scanner.engine._causal_rows(raw, now)
    reasons = []
    if observed.day != now.date() or not observed.opening_ok:
        reasons.append("HISTORICAL_OPENING_FIRST_TICK_MISSING_OR_LATE")
    if observed.halted:
        reasons.append("HISTORICAL_TRADING_PAUSED")
    if not _finite(feature.get("vwap")) or feature.get("vwap", 0) <= 0:
        reasons.append("HISTORICAL_VWAP_UNAVAILABLE")
    if not rows:
        reasons.append("HISTORICAL_TICK_UNAVAILABLE")
    else:
        max_age = float(scanner.config.get("tick_stale_seconds", 5))
        exchange_age = (now - rows[-1]["time"]).total_seconds()
        receipt_age = (now - rows[-1]["received_at"]).total_seconds()
        if exchange_age < -0.5 or exchange_age > max_age or receipt_age < 0 or receipt_age > max_age:
            reasons.append("HISTORICAL_TICK_STALE")
    cutoff = now - timedelta(seconds=300)
    reference = [row for row in rows if row["time"] <= cutoff]
    return {"features": feature,
            "historical_vwap_available": not reasons,
            "historical_vwap_unavailable_reasons": reasons,
            "last_exchange_at": rows[-1]["time"].isoformat() if rows else None,
            "last_received_at": rows[-1]["received_at"].isoformat() if rows else None,
            "return_300s_reference_exchange_at": reference[-1]["time"].isoformat() if reference else None,
            "return_300s_reference_received_at": reference[-1]["received_at"].isoformat() if reference else None,
            "return_300s_reference_age_from_target_seconds": (cutoff - reference[-1]["time"]).total_seconds() if reference else None,
            "saved_receipt_cursor": now.isoformat()}


def build_feature_context(data, signals, config, decision_times=None):
    """Annotate archived S1 rows and expose per-signal/common market masks.

    ``data`` is the unchanged normalized ``data.load_day`` output. ``signals``
    contains its archived round-one raw S1 rows, not portfolio-adopted signals.
    ``config`` is the original flat round-one scanner config.  All return values
    are JSON-serializable; caller input dictionaries and events are not changed.

    By default market_decision_mask covers the original 30-second entry grid.
    An empty decision_times iterable disables that optional full-grid inventory.
    A common mask is stock/signal-specific: another stock's missing observations
    must not disqualify an otherwise evaluable signal.
    """
    originals = list(signals)
    if any(row.get("entry_model") != "S1" for row in originals):
        raise ValueError("round-two features accept unchanged raw S1 signals only")
    if len({row["signal_id"] for row in originals}) != len(originals):
        raise ValueError("duplicate signal_id in raw S1 inputs")
    events = data["events"]
    tz = events[0]["received_at"].tzinfo if events else _stamp(originals[0]["time"]).tzinfo if originals else None
    market_times = list(decision_times) if decision_times is not None else _grid(data["day"], tz) if tz else []
    market_times = {_stamp(at) for at in market_times}
    queries = defaultdict(set)
    for row in originals:
        now = _stamp(row["time"])
        if now.strftime("%Y%m%d") != data["day"]:
            raise ValueError("signal date differs from archive day")
        queries[now].add(str(row["symbol"]))
        queries[now - timedelta(seconds=VWAP_HISTORY_SECONDS)].add(str(row["symbol"]))
        market_times.add(now)
    scanner = SignalScanner(data["metadata"], data["candidates"], config)
    saved, markets, cursor = {}, {}, 0
    previous_receipt = None
    # Fail on a caller that bypasses the reader's receipt sort.  We must not
    # silently consume a late record only after the cursor has passed its time.
    for event in events:
        received = _stamp(event["received_at"])
        if previous_receipt is not None and received < previous_receipt:
            raise ValueError("events must be sorted by receipt before feature replay")
        previous_receipt = received
    for now in sorted(set(queries) | market_times):
        while cursor < len(events) and _stamp(events[cursor]["received_at"]) <= now:
            scanner.ingest(events[cursor])
            cursor += 1
        scanner._drain(now)
        for symbol in sorted(queries.get(now, ())):
            saved[(now, symbol)] = _stock_snapshot(scanner, symbol, now)
        if now in market_times:
            markets[now] = _market_snapshot(scanner, now)
    enriched, masks = [], []
    for original in originals:
        row = deepcopy(original)
        now, symbol = _stamp(row["time"]), str(row["symbol"])
        earlier = now - timedelta(seconds=VWAP_HISTORY_SECONDS)
        current, history, market = saved[(now, symbol)], saved[(earlier, symbol)], markets[now]
        archived, recomputed = row.get("features", {}), current["features"]
        shared_fields = ("price", "vwap", "stock_return_5m", "relative_strength_5m",
                         "benchmark_return_5m", "benchmark_vwap_gap", "market_filter_pass")
        for name in shared_fields:
            if name not in archived:
                raise FeatureMismatchError("ARCHIVED_SHARED_FEATURE_MISSING:" + name)
            if not _same(archived[name], recomputed.get(name)):
                raise FeatureMismatchError("ARCHIVED_SHARED_FEATURE_MISMATCH:" + name + ":" + row["signal_id"])
        current_vwap, past_vwap = archived["vwap"], history["features"].get("vwap")
        slope_available = history["historical_vwap_available"] and _finite(current_vwap)
        rs = archived.get("relative_strength_5m")
        r2_reasons = list(market["unavailable_reasons"])
        if not _finite(archived.get("stock_return_5m")):
            r2_reasons.append("STOCK_300S_RETURN_UNAVAILABLE")
        if not _finite(rs):
            r2_reasons.append("RS_300_UNAVAILABLE")
        original_distance = archived.get("entry_distance_fraction")
        reference, price = archived.get("retest_reference"), archived.get("price")
        if _finite(reference) and reference > 0 and _finite(price):
            calculated_distance = (reference - price) / reference
            if not _same(original_distance, calculated_distance):
                raise FeatureMismatchError("ARCHIVED_ENTRY_DISTANCE_MISMATCH:" + row["signal_id"])
        else:
            calculated_distance = None
        row["round2_features"] = {
            "version": "SIX_FIXED_FILTERS_RECEIPT_CAUSAL_V1",
            "shared_feature_verification": "MATCH_REPLAYED_ORIGINAL_SCANNER",
            "original_signal_id": row["signal_id"],
            "current_receipt_cursor": now.isoformat(),
            "market_available": market["market_available"],
            "market_unavailable_reasons": list(market["unavailable_reasons"]),
            "benchmark_price": market["benchmark_price"],
            "benchmark_vwap": market["benchmark_vwap"],
            "benchmark_vwap_gap": archived["benchmark_vwap_gap"],
            "benchmark_return_300s": archived["benchmark_return_5m"],
            "market_weak": archived["market_filter_pass"],
            "market_regime": market["market_regime"],
            "benchmark_last_exchange_at": market["benchmark_last_exchange_at"],
            "benchmark_last_received_at": market["benchmark_last_received_at"],
            "benchmark_300s_reference_exchange_at": market["benchmark_300s_reference_exchange_at"],
            "benchmark_300s_reference_received_at": market["benchmark_300s_reference_received_at"],
            "benchmark_300s_reference_age_from_target_seconds": market["benchmark_300s_reference_age_from_target_seconds"],
            "stock_return_300s": archived["stock_return_5m"],
            "stock_300s_reference_exchange_at": current["return_300s_reference_exchange_at"],
            "stock_300s_reference_received_at": current["return_300s_reference_received_at"],
            "stock_300s_reference_age_from_target_seconds": current["return_300s_reference_age_from_target_seconds"],
            "rs_300": rs, "rs_available": not r2_reasons,
            "rs_unavailable_reasons": r2_reasons,
            "vwap_now": current_vwap, "vwap_180s_ago": past_vwap if slope_available else None,
            "vwap_180s_ago_snapshot_at": earlier.isoformat(),
            "vwap_180s_ago_last_exchange_at": history.get("last_exchange_at"),
            "vwap_180s_ago_last_received_at": history.get("last_received_at"),
            "vwap_180s_ago_warmup_reason": history["features"].get("warmup_reason"),
            "vwap_slope_available": slope_available,
            "vwap_slope_unavailable_reasons": list(history["historical_vwap_unavailable_reasons"]),
            "vwap_change_180s": current_vwap - past_vwap if slope_available else None,
            "vwap_down_180s": current_vwap < past_vwap if slope_available else None,
            "entry_distance_fraction": original_distance,
            "entry_distance_recomputed": calculated_distance,
            "direction_basis": "VENDOR_IN_OUT_0_1_PROXY_NOT_CERTIFIED_ACTUAL_INITIATOR",
            "strict_executable": False,
            "strict_result": "NA_UNVERIFIED_UNITS_ELIGIBILITY_FEES_TIMESTAMPS",
        }
        enriched.append(row)
        evaluations = {variant: evaluate_variant(row, variant) for variant in VARIANTS}
        missing = {variant: value["reasons"] for variant, value in evaluations.items() if not value["evaluable"]}
        masks.append({"signal_id": row["signal_id"], "symbol": symbol, "time": now.isoformat(),
                      "common_evaluable": not missing, "unavailable_by_variant": missing,
                      "basis": "ALL_SIX_AVAILABILITY_ONLY_NOT_ALL_SIX_PASS"})
    return {"signals": enriched, "common_mask": masks,
            "market_decision_mask": [markets[now] for now in sorted(markets)],
            "replay_scope": "RAW_S1_SIGNALS_AND_CAUSAL_FEATURES_NO_PERFORMANCE",
            "actual_orders": 0, "broker_connections": 0, "market_subscriptions": 0}


def build_signal_features(data, signals, config):
    """Compatibility convenience API; use build_feature_context for masks."""
    return build_feature_context(data, signals, config, decision_times=[])["signals"]


def evaluate_variant(signal, variant):
    """Return a three-state predicate: unavailable is never a failed condition.

    ``pass`` is None when unevaluable, False when evaluable but blocked, and True
    when accepted.  No eligibility/account/capacity claim is made by this signal
    predicate.  Known trading prohibitions belong to the separate portfolio gate.
    """
    if variant not in VARIANTS:
        raise ValueError("only predeclared R0..R5 variants are permitted")
    if signal.get("entry_model") != "S1":
        raise ValueError("filter input must be an unchanged raw S1 signal")
    feature = signal.get("round2_features", {})
    unavailable, failures = [], []
    if variant in {"R1", "R5"}:
        if not feature.get("market_available", False) or feature.get("market_weak") is None:
            unavailable.extend(feature.get("market_unavailable_reasons") or ["BENCHMARK_0050_UNAVAILABLE"])
        elif feature["market_weak"] is not True:
            failures.append("MARKET_NOT_WEAK")
    if variant in {"R2", "R5"}:
        if not feature.get("rs_available", False) or not _finite(feature.get("rs_300")):
            unavailable.extend(feature.get("rs_unavailable_reasons") or ["RS_300_UNAVAILABLE"])
        elif feature["rs_300"] > RS300_MAXIMUM:
            failures.append("RS_300_ABOVE_MINUS_0_002")
    if variant == "R3":
        if not feature.get("vwap_slope_available", False) or feature.get("vwap_down_180s") is None:
            unavailable.extend(feature.get("vwap_slope_unavailable_reasons") or ["VWAP_180S_HISTORY_UNAVAILABLE"])
        elif feature["vwap_down_180s"] is not True:
            failures.append("VWAP_NOT_FALLING_OVER_180S")
    if variant == "R4":
        if not _finite(feature.get("entry_distance_fraction")):
            unavailable.append("RETEST_ENTRY_DISTANCE_UNAVAILABLE")
        elif feature["entry_distance_fraction"] > ENTRY_DISTANCE_MAXIMUM + 1e-12:
            failures.append("ENTRY_DISTANCE_ABOVE_0_003")
    if unavailable:
        return {"variant": variant, "evaluable": False, "pass": None,
                "status": "UNAVAILABLE", "reasons": sorted(set(unavailable)),
                "known_condition_failures": failures, "features": feature}
    return {"variant": variant, "evaluable": True, "pass": not failures,
            "status": "PASS" if not failures else "BLOCKED_CONDITION",
            "reasons": failures or ["UNCHANGED_R0_S1_BASE" if variant == "R0" else "FIXED_ADDITIONAL_CONDITION_PASSED"],
            "known_condition_failures": failures, "features": feature}
