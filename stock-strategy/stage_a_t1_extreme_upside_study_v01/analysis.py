"""Frozen, short-sample Stage A T+1 extreme-upside diagnostic."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import csv
import hashlib
import json
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import numpy as np

from prospective_shadow_v01.market_data_provider import ExistingDailyDataProvider
from stage_a_prospective_watchlist_v01.watchlist import build_watchlist
from stage_a_prospective_watchlist_v01.seal_store import RUNTIME_DIR as STAGE_A_RUNTIME
from surge_event_study_v01.features import iter_signal_dates
from winner_coverage_taxonomy_v01.features import build_taxonomy_features

from .entry_state import classify, canonical, digest
from .official_limits import twse_limits, tpex_next_limits
from .outcomes import episode, evaluate, next_session_bar, rank_bucket


HERE = Path(__file__).resolve().parent
SIGNAL_DATES = ("20260909", "20260910", "20260911", "20260914", "20260915", "20260916", "20260917")
EXTRA_CONTEXT_DATE = "20260908"
METRICS = ("t1_close_at_upper_limit", "t1_high_touched_upper_limit", "t1_return_ge_8", "t1_return_ge_5", "t1_return_ge_3", "capturable_3", "capturable_5")


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_seal(day: str) -> dict | None:
    path = STAGE_A_RUNTIME / "seals" / f"{day}.json"
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    keys = ("schema_version", "signal_date", "setup", "mode", "stocks", "model_hash", "model_spec_hash", "config_hash", "input_hash", "eligible_stock_count")
    if value.get("seal_hash") != digest({key: value[key] for key in keys}) or len(value["stocks"]) != 30:
        raise RuntimeError(f"Stage A seal invalid: {day}")
    return value


def _summary(rows: list[dict]) -> dict:
    result = {"n": len(rows)}
    for metric in METRICS:
        result[metric + "_count"] = sum(bool(row[metric]) for row in rows)
        result[metric + "_rate"] = result[metric + "_count"] / len(rows) if rows else None
    for metric in ("t1_close_return", "t1_open_return", "open_to_close_return", "max_upside_from_open", "max_drawdown_from_open"):
        result[metric + "_mean"] = mean(row[metric] for row in rows) if rows else None
        result[metric + "_median"] = median(row[metric] for row in rows) if rows else None
    return result


def _csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        return b"\n"
    import io
    buffer = io.StringIO(newline="")
    columns = list(dict.fromkeys(key for row in rows for key in row))
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _write_immutable(path: Path, content: bytes) -> str:
    with path.open("xb") as handle:
        handle.write(content)
    return hashlib.sha256(content).hexdigest()


def _random_null(by_day: dict[str, list[dict]], actual: list[dict], reps: int = 10000) -> list[dict]:
    rng = np.random.default_rng(20260920)
    columns = ("t1_close_at_upper_limit", "t1_high_touched_upper_limit", "t1_return_ge_8", "t1_return_ge_5", "capturable_5")
    arrays = {day: np.asarray([[int(row[name]) for name in columns] for row in rows], dtype=np.int16) for day, rows in by_day.items()}
    observed = np.asarray([sum(int(row[name]) for row in actual) for name in columns])
    null = np.zeros((reps, len(columns)), dtype=np.int16)
    for i in range(reps):
        for values in arrays.values():
            if len(values) < 30:
                raise RuntimeError("random null day has fewer than 30 eligible outcomes")
            null[i] += values[rng.choice(len(values), 30, replace=False)].sum(axis=0)
    return [{"metric": name, "observed_hits": int(observed[j]), "null_mean_hits": float(null[:, j].mean()), "null_p95_hits": float(np.quantile(null[:, j], .95)), "observed_percentile": float((null[:, j] <= observed[j]).mean()), "simulations": reps} for j, name in enumerate(columns)]


def _cluster_bootstrap(selected: list[dict], market: list[dict], reps: int = 5000) -> list[dict]:
    rng = np.random.default_rng(20260921)
    days = sorted({row["signal_date"] for row in selected})
    sday = {day: [row for row in selected if row["signal_date"] == day] for day in days}
    mday = {day: [row for row in market if row["signal_date"] == day] for day in days}
    result = []
    for cluster in ("signal_date", "stock_id"):
        if cluster == "signal_date":
            keys = days
            selected_groups = sday
            market_groups = mday
        else:
            keys = sorted({row["stock_id"] for row in selected})
            selected_groups = {key: [row for row in selected if row["stock_id"] == key] for key in keys}
            # Stock-cluster robustness conditions on the observed market rate;
            # selected-stock resampling cannot recreate an all-universe draw.
            market_groups = {}
        for metric in ("t1_close_at_upper_limit", "t1_high_touched_upper_limit", "capturable_5"):
            differences = []
            for _ in range(reps):
                draw = rng.choice(keys, len(keys), replace=True)
                s = [row for key in draw for row in selected_groups[key]]
                if cluster == "signal_date":
                    m = [row for key in draw for row in market_groups[key]]
                    comparator = mean(row[metric] for row in m)
                else:
                    comparator = mean(row[metric] for row in market)
                differences.append(mean(row[metric] for row in s) - comparator)
            result.append({"comparison": "STAGE_A_MINUS_MARKET" if cluster == "signal_date" else "STAGE_A_MINUS_FIXED_MARKET_RATE", "cluster": cluster, "metric": metric, "estimate": mean(row[metric] for row in selected) - mean(row[metric] for row in market), "ci_low": float(np.quantile(differences, .025)), "ci_high": float(np.quantile(differences, .975)), "reps": reps})
    ready = [row for row in selected if row["classification"] == "READY"]
    hot = [row for row in selected if row["classification"] == "OVERHEATED"]
    if ready and hot:
        for metric in ("t1_close_at_upper_limit", "max_drawdown_from_open", "max_upside_from_open"):
            values = []
            for _ in range(reps):
                draw = rng.choice(days, len(days), replace=True)
                r = [row for day in draw for row in sday[day] if row["classification"] == "READY"]
                h = [row for day in draw for row in sday[day] if row["classification"] == "OVERHEATED"]
                if r and h:
                    values.append(mean(row[metric] for row in r) - mean(row[metric] for row in h))
            result.append({"comparison": "READY_MINUS_OVERHEATED", "cluster": "signal_date", "metric": metric, "estimate": mean(row[metric] for row in ready) - mean(row[metric] for row in hot), "ci_low": float(np.quantile(values, .025)), "ci_high": float(np.quantile(values, .975)), "reps": reps})
    return result


def run(active_inputs_path: Path, direct_audit_path: Path, *, out_dir: Path = HERE) -> dict:
    if any((out_dir / name).exists() for name in ("validation_summary.json", "run_manifest.json", "signal_observations.csv")):
        raise RuntimeError("study already published; immutable outputs cannot be overwritten")
    active = json.loads(active_inputs_path.read_text(encoding="utf-8"))
    if active["target_date"] != "20260918":
        raise RuntimeError("study requires the fixed 9/18 pre-outcome archive")
    archives = [Path(value) for value in active["archives"]]
    calendar_path = Path(active["trading_calendar"])
    provider = ExistingDailyDataProvider(archives, trading_calendar_path=calendar_path)
    full = provider.load_through("20260918")
    if full.data_through_date != "20260918" or "20260921" in full.benchmark.calendar:
        raise RuntimeError("9/21 outcome may not be present")
    audit = json.loads(direct_audit_path.read_text(encoding="utf-8"))
    stocks = {stock.code: stock for stock in full.prepared_stocks}
    official = {}
    limit_sources = {}
    cache = HERE / "runtime" / "official_limits_all"
    for day in SIGNAL_DATES:
        next_day = full.benchmark.calendar[full.benchmark.calendar.index(day) + 1]
        listed, provenance = twse_limits(next_day, cache)
        otc, otc_provenance = tpex_next_limits(day, audit)
        duplicate = set(listed).intersection(otc)
        if duplicate:
            raise RuntimeError(f"cross-market official limit identity collision on {day}: {sorted(duplicate)[:3]}")
        official[day] = {**listed, **otc}
        limit_sources[day] = {"twse": provenance["sha256"], "tpex": otc_provenance["sha256"]}
    watchlists = {}
    seals = {}
    for day in (EXTRA_CONTEXT_DATE, *SIGNAL_DATES, "20260918"):
        seal = _stage_seal(day)
        if seal is None:
            if day >= "20260916":
                raise RuntimeError(f"missing prospective Stage A seal: {day}")
            truncated = provider.load_through(day)
            seal = build_watchlist(truncated, day)
        watchlists[day] = seal["stocks"]
        seals[day] = seal
    rows = []
    market = []
    market_by_day = {}
    streaks = {}
    previous = {item["stock_id"]: 1 for item in watchlists[EXTRA_CONTEXT_DATE]}
    for day in SIGNAL_DATES:
        selected = {item["stock_id"]: item for item in watchlists[day]}
        if len(selected) != 30:
            raise RuntimeError("Stage A daily Top30 count invalid")
        universe = []
        blocks = list(iter_signal_dates(full.prepared_stocks, full.benchmark, day, day))
        if len(blocks) != 1:
            raise RuntimeError("market calendar alignment failed")
        for observation, stock, index in blocks[0][2]:
            try:
                build_taxonomy_features(stock, index, full.benchmark)
            except ValueError:
                continue
            code = str(observation.code)
            outcome_bar = next_session_bar(stock, day, full.benchmark.calendar)
            upper = official[day].get(code)
            if outcome_bar is None or upper is None:
                # Non-evaluable market rows are explicitly tracked, never called zero.
                continue
            outcome = evaluate(observation.signal_close, outcome_bar, upper)
            base = {"signal_date": day, "stock_id": code, "stock_name": observation.name, "signal_close": observation.signal_close, **outcome}
            universe.append(base)
            if code in selected:
                item = selected[code]
                state = classify(stock.bars[:index + 1], day)
                episode_label, length = episode(previous.get(code, 0))
                rows.append({**base, "stage_a_rank": item["rank"], "stage_a_score": item["score"], "rank_bucket": rank_bucket(item["rank"]), "top30_streak_length": length, "episode": episode_label, "classification": state.classification, "classification_evidence": "POST_HOC_EXPLORATORY_CLASSIFICATION", "signal_evidence": "PROSPECTIVE_SEALED" if day >= "20260916" else "RETROSPECTIVE_CAUSAL_RECONSTRUCTION", "signal_seal_hash": seals[day].get("seal_hash", ""), "atr14": state.atr14, "ret_1d": state.ret_1d, "ret_3d": state.ret_3d, "ret_5d": state.ret_5d, "ma5": state.ma5, "ma20": state.ma20, "ma20_5d_ago": state.ma20_5d_ago, "ma5_atr_extension": state.ma5_atr_extension, "ma20_atr_extension": state.ma20_atr_extension})
        if len([row for row in rows if row["signal_date"] == day]) != 30:
            raise RuntimeError(f"not all Stage A Top30 outcomes evaluable on {day}")
        market += universe
        market_by_day[day] = universe
        previous = {code: previous.get(code, 0) + 1 for code in selected}
    signal_date_summary = []
    for day in SIGNAL_DATES:
        selected_day = [row for row in rows if row["signal_date"] == day]
        base_day = market_by_day[day]
        s, m = _summary(selected_day), _summary(base_day)
        hit = "t1_close_at_upper_limit"
        signal_date_summary.append({"signal_date": day, "outcome_date": selected_day[0]["outcome_date"], "stage_a_count": 30, "stage_a_close_limit_hits": s[hit + "_count"], "stage_a_touch_limit_hits": s["t1_high_touched_upper_limit_count"], "stage_a_ge8_hits": s["t1_return_ge_8_count"], "stage_a_ge5_hits": s["t1_return_ge_5_count"], "stage_a_ge3_hits": s["t1_return_ge_3_count"], "eligible_universe_count": m["n"], "market_close_limit_hits": m[hit + "_count"], "stage_a_close_limit_rate": s[hit + "_rate"], "market_close_limit_rate": m[hit + "_rate"], "daily_lift": s[hit + "_rate"] / m[hit + "_rate"] if m[hit + "_rate"] else None, "market_limit_up_coverage": s[hit + "_count"] / m[hit + "_count"] if m[hit + "_count"] else None})
    group = lambda key: [{key: label, **_summary([row for row in rows if row[key] == label])} for label in sorted({row[key] for row in rows})]
    stock_rows = [{"stock_id": code, "stock_name": items[0]["stock_name"], "number_of_stage_a_days": len(items), "number_of_t1_limit_close": sum(row["t1_close_at_upper_limit"] for row in items), "number_of_t1_limit_touch": sum(row["t1_high_touched_upper_limit"] for row in items)} for code, items in sorted(((code, [row for row in rows if row["stock_id"] == code]) for code in {row["stock_id"] for row in rows}))]
    stock_rows.sort(key=lambda row: (-row["number_of_t1_limit_close"], -row["number_of_stage_a_days"], row["stock_id"]))
    market_hit_rate = _summary(market)["t1_close_at_upper_limit_rate"]
    leave_one = []
    for item in stock_rows[:5]:
        remaining = [row for row in rows if row["stock_id"] != item["stock_id"]]
        rate = _summary(remaining)["t1_close_at_upper_limit_rate"]
        leave_one.append({"excluded_stock_id": item["stock_id"], "excluded_stock_name": item["stock_name"], "remaining_n": len(remaining), "remaining_close_limit_rate": rate, "lift_vs_eligible_market": rate / market_hit_rate if market_hit_rate else None})
    capturability = [{"opening_bucket": bucket, **_summary([row for row in rows if row["t1_close_at_upper_limit"] and row["close_limit_open_bucket"] == bucket])} for bucket in ("OPEN_AT_LIMIT", "OPEN_GE_8", "OPEN_5_TO_8", "OPEN_3_TO_5", "OPEN_LT_3")]
    for name, content in {
        "signal_observations.csv": rows,
        "signal_date_summary.csv": signal_date_summary,
        "market_baseline.csv": [{"signal_date": day, **_summary(market_by_day[day])} for day in SIGNAL_DATES],
        "rank_bucket_analysis.csv": group("rank_bucket"),
        "new_vs_repeat_analysis.csv": group("episode"),
        "stock_contribution.csv": stock_rows,
        "leave_one_stock_out.csv": leave_one,
        "t1_open_capturability.csv": capturability,
        "entry_state_analysis.csv": group("classification"),
        "random_top30_null.csv": _random_null(market_by_day, rows),
        "bootstrap_summary.csv": _cluster_bootstrap(rows, market),
    }.items():
        _write_immutable(out_dir / name, _csv_bytes(content))
    pooled = _summary(rows)
    market_summary = _summary(market)
    result = {"study_id": "STAGE_A_T1_EXTREME_UPSIDE_STUDY_V0_1", "status": "SHORT_SAMPLE", "signal_dates": list(SIGNAL_DATES), "evaluable_signal_days": len(SIGNAL_DATES), "stage_a": pooled, "eligible_market": market_summary, "pooled_close_limit_lift": pooled["t1_close_at_upper_limit_rate"] / market_summary["t1_close_at_upper_limit_rate"] if market_summary["t1_close_at_upper_limit_rate"] else None, "market_limit_up_coverage": pooled["t1_close_at_upper_limit_count"] / market_summary["t1_close_at_upper_limit_count"] if market_summary["t1_close_at_upper_limit_count"] else None, "daily_median_hit_rate": median(row["stage_a_close_limit_rate"] for row in signal_date_summary), "positive_lift_days": sum(row["daily_lift"] is not None and row["daily_lift"] > 1 for row in signal_date_summary), "classification_seal_20260918": json.loads((HERE / "runtime/seals/20260918.json").read_text(encoding="utf-8"))["seal_hash"], "outcome_20260918_to_20260921": "NOT_MATURED_NOT_EVALUATED", "stage_a_refit_count": 0, "actual_orders": 0, "actual_fills": 0, "broker_connections": 0, "official_limit_source_hashes": limit_sources}
    _write_immutable(out_dir / "validation_summary.json", canonical(result) + b"\n")
    artifacts = {path.name: _file_sha(path) for path in out_dir.glob("*.csv")}
    for name in ("validation_summary.json", "analysis_spec.json", "prospective_status.json", "README.md"):
        if (out_dir / name).exists():
            artifacts[name] = _file_sha(out_dir / name)
    manifest = {"study_id": result["study_id"], "status": "COMPLETE_SHORT_SAMPLE", "active_input_sha256": _file_sha(active_inputs_path), "direct_official_audit_sha256": _file_sha(direct_audit_path), "artifacts_sha256": artifacts, "no_20260921_input": True}
    _write_immutable(out_dir / "run_manifest.json", canonical(manifest) + b"\n")
    return result
