from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import statistics

from surge_event_study_v01.config import CFG as SURGE_CFG
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file
from stock_specific_setup_discovery_v01.config import SETUP_DEFINITIONS
from stock_specific_setup_discovery_v01.setups import setup_flags

from .analysis import (
    benchmark_context, classify, cluster_bootstrap, continuous_rows, decile_rows,
    delta_summary, evaluate_outcome, stock_contexts, summarize, validate_cost_contract,
)
from .config import CFG, PERIODS, PRIMARY_GROUPS, RATIO_BUCKETS
from .pivots import primary_group, ratio_bucket, structures_by_signal_index


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01" / "run_manifest.json"
HIGH_UPSIDE_DIR = STOCK_STRATEGY / "high_upside_swing_specialist_universe_v01"
HIGH_UPSIDE_FINAL = HIGH_UPSIDE_DIR / "final_high_upside_specialist_universe.csv"
HIGH_UPSIDE_ELIGIBILITY = HIGH_UPSIDE_DIR / "eligibility_universe.csv"
HIGH_UPSIDE_MANIFEST = HIGH_UPSIDE_DIR / "run_manifest.json"
SETUP_DIR = STOCK_STRATEGY / "stock_specific_setup_discovery_v01"
SETUP_DEFINITIONS_PATH = SETUP_DIR / "setup_definitions.json"
SETUP_ANALYSIS_SPEC = SETUP_DIR / "analysis_spec.json"
SETUP_RUN_MANIFEST = SETUP_DIR / "run_manifest.json"

OUTPUTS = (
    "analysis_spec.json", "setup_source_manifest.json", "signal_observations.csv",
    "confirmed_pivots.csv", "measured_move_structures.csv",
    "completion_ratio_distribution.csv", "bucket_performance.csv",
    "continuous_relationship.csv", "decile_analysis.csv", "family_robustness.csv",
    "stock_robustness.csv", "period_comparison.csv", "cluster_bootstrap_summary.csv",
    "sensitivity_3x3.csv", "coverage_summary.csv", "lookahead_audit.json",
    "validation_summary.json", "run_manifest.json",
)
SOURCE_FILES = (
    "__init__.py", "config.py", "pivots.py", "analysis.py", "main.py",
    "README.md", "tests/test_study.py",
)
ACTIVE_STATUSES = {
    "PERSISTENT_HIGH_UPSIDE_SPECIALIST", "REGIME_HIGH_UPSIDE_SPECIALIST",
    "DISCOVERY_ONLY_HIGH_UPSIDE",
}


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError(f"refusing to publish empty table: {path.name}")
    fields = []
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def canonical_hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def input_sources() -> tuple[list[Path], list[Path], list[dict]]:
    manifest = json.loads(WINNER_MANIFEST.read_text(encoding="utf-8"))
    accepted = {f"yearly_{year}.zip" for year in range(2019, 2026)} | {"twse_price_supplement.csv"}
    archives, supplements, hashes = [], [], []
    for item in manifest["input_hashes"]:
        raw = Path(item["path"])
        path = raw if raw.is_absolute() else STOCK_STRATEGY / raw
        if path.name not in accepted:
            continue
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            raise RuntimeError(f"formal OHLCV input missing or drifted: {path}")
        hashes.append({"path": str(path.relative_to(STOCK_STRATEGY)), "bytes": path.stat().st_size, "sha256": item["sha256"]})
        (archives if path.suffix == ".zip" else supplements).append(path)
    if [p.name for p in archives] != [f"yearly_{year}.zip" for year in range(2019, 2026)]:
        raise RuntimeError("expected exact immutable yearly 2019-2025 archives")
    if [p.name for p in supplements] != ["twse_price_supplement.csv"]:
        raise RuntimeError("expected exact immutable supplement")
    return archives, supplements, hashes


def frozen_universes() -> tuple[list[dict], list[str], dict]:
    manifest = json.loads(HIGH_UPSIDE_MANIFEST.read_text(encoding="utf-8"))
    audits = {}
    for path in (HIGH_UPSIDE_FINAL, HIGH_UPSIDE_ELIGIBILITY):
        expected = manifest["output_hashes"][path.name]
        actual = sha256_file(path)
        if expected != actual:
            raise RuntimeError(f"frozen universe source drifted: {path}")
        audits[path.name] = {"path": str(path.relative_to(STOCK_STRATEGY)), "sha256": actual}
    with HIGH_UPSIDE_FINAL.open(encoding="utf-8-sig", newline="") as handle:
        active = [row for row in csv.DictReader(handle) if row["final_classification"] in ACTIVE_STATUSES]
    with HIGH_UPSIDE_ELIGIBILITY.open(encoding="utf-8-sig", newline="") as handle:
        eligible = sorted(row["stock_id"] for row in csv.DictReader(handle) if row["eligible"].lower() == "true")
    if len(active) != 9 or len(eligible) != 133:
        raise RuntimeError(f"frozen universe cardinality drifted: active={len(active)}, eligible={len(eligible)}")
    audits["active_count"] = len(active)
    audits["secondary_count"] = len(eligible)
    audits["active_stock_ids"] = [row["stock_id"] for row in active]
    return active, eligible, audits


def setup_source_manifest() -> dict:
    paths = (SETUP_DEFINITIONS_PATH, SETUP_ANALYSIS_SPEC, SETUP_RUN_MANIFEST, SETUP_DIR / "config.py", SETUP_DIR / "setups.py")
    source_manifest = json.loads(SETUP_RUN_MANIFEST.read_text(encoding="utf-8"))
    expected_definition = source_manifest["output_hashes"]["setup_definitions.json"]
    if sha256_file(SETUP_DEFINITIONS_PATH) != expected_definition:
        raise RuntimeError("frozen setup_definitions.json drifted")
    definitions = json.loads(SETUP_DEFINITIONS_PATH.read_text(encoding="utf-8"))["definitions"]
    if len(definitions) != 16 or [row["setup_id"] for row in definitions] != [row[0] for row in SETUP_DEFINITIONS]:
        raise RuntimeError("expected exact frozen 16-variant setup set")
    return {
        "study_id": CFG.study_id,
        "source_study_id": "STOCK_SPECIFIC_SETUP_DISCOVERY_V0_1",
        "source_files": [{"path": str(path.relative_to(STOCK_STRATEGY)), "sha256": sha256_file(path)} for path in paths],
        "definition_count": len(definitions),
        "setup_ids": [row["setup_id"] for row in definitions],
        "all_predefined_setup_observations_used": True,
        "prior_primary_setup_selection_used": False,
        "measured_move_results_used_to_select_setups": False,
    }


def analysis_spec(inputs: list[dict], universe: dict, setups: dict) -> dict:
    return {
        "study_id": CFG.study_id,
        "status": "FROZEN_BEFORE_FORMAL_RESULTS",
        "concept_status": "CONCEPT_RECONSTRUCTION_NOT_AUTHOR_STRATEGY",
        "config": CFG.snapshot(),
        "config_fingerprint": CFG.fingerprint(),
        "input_hashes": inputs,
        "universe_source": universe,
        "setup_source_manifest_sha256": canonical_hash(setups),
        "causal_pivot": {
            "primary": "2-left/2-right; pivot usable only when pivot_index+2 confirmation_date <= signal T",
            "sensitivity_only": "3-left/3-right; never replaces primary",
            "high": "High_i > both left highs and >= both right highs",
            "low": "Low_i < both left lows and <= both right lows",
            "tie_handling": "same-type equal price retains earlier confirmed pivot; raw ties order by confirmation index, pivot index, LOW then HIGH",
            "compression": "consecutive highs retain higher; consecutive lows retain lower",
            "abc": "most recent completed consecutive LOW-HIGH-LOW in causal compressed sequence",
        },
        "measured_move": {
            "length": "B_price-A_price", "target_d": "C_price+(B_price-A_price)",
            "completion_ratio": "(Close_T-C_price)/(B_price-A_price)",
            "structural_validity": "A_index<B_index<C_index; B>A; A<C<B; each leg >=1 session; all confirmation indices <=T",
            "below_c": "negative R retained and marked BELOW_C",
        },
        "ratio_buckets": [list(item) for item in RATIO_BUCKETS],
        "primary_groups": [list(item) for item in PRIMARY_GROUPS],
        "outcomes": {
            "entry": "T+1 regular-session Open", "paths": "Day1-Day10 Close",
            "full_path": "same discontinuity segment, consecutive market sessions, wholly inside named period",
            "costs": validate_cost_contract(),
        },
        "continuous_analysis": "Spearman only; deciles are equal-count pooled within scope and period with deterministic tie order",
        "bootstrap": {
            "schemes": ["signal_date_cluster", "calendar_month_cluster"], "iterations": CFG.bootstrap_iterations,
            "statistics": "cluster-resampled means/rates and net-PF differences; A-B and C-B",
        },
        "classification": {
            "too_sparse": "primary measured-move coverage <30% or primary A/B evaluable n <100 in any named period",
            "edge": "discovery A-B improves mean MFE10, D5 net and downside-first, and each later period keeps >=2/3 directions",
            "u_shape": "discovery A-B and C-B each keep >=2/3 directions and later C-B keeps >=2/3",
            "regime": "discovery all three A-B directions but only one later period keeps >=2/3",
            "risk_path": "A-B downside-first improves in all periods without a full return edge",
            "family_specific": "exactly one family/period supports >=2/3 A-B directions when pooled rules do not pass",
        },
        "market_context_used_as_gate": False,
        "future_outcomes_used_for_structure_construction": False,
        "later_periods_used_to_change_buckets_or_pivots": False,
        "threshold_optimization_count": 0,
        "model_fit_count": 0,
        "stage_a_refit_count": 0,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }


def freeze_spec() -> dict:
    _, _, inputs = input_sources()
    _, _, universe = frozen_universes()
    setups = setup_source_manifest()
    payload = analysis_spec(inputs, universe, setups)
    for name, content in (("analysis_spec.json", payload), ("setup_source_manifest.json", setups)):
        path = ROOT / name
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != content:
                raise RuntimeError(f"frozen {name} drifted")
        else:
            write_json(path, content)
    return payload


def _period_for(date: str) -> tuple[str, str] | None:
    for label, start, end in PERIODS:
        if start <= date <= end:
            return label, end
    return None


def _volume_ratio(stock, position: int) -> float | None:
    if position < 20:
        return None
    values = [stock.bars[i].volume for i in range(position - 20, position)]
    median = statistics.median(values)
    return stock.bars[position].volume / median if median > 0 else None


def _scope_rows(observations: list[dict], active_codes: set[str], scope: str, period: str) -> list[dict]:
    rows = [row for row in observations if row["period"] == period]
    if scope == "PRIMARY_ACTIVE_SPECIALISTS":
        rows = [row for row in rows if row["stock_id"] in active_codes]
    return rows


def _dimension_groups(rows: list[dict], dimension: str):
    if dimension == "POOLED":
        return [("ALL", rows)]
    field = {"SETUP_FAMILY": "setup_family", "STOCK": "stock_id", "SETUP_ID": "setup_id"}[dimension]
    return [(key, [row for row in rows if row[field] == key]) for key in sorted({row[field] for row in rows})]


def publish() -> dict:
    spec = freeze_spec()
    existing = [name for name in OUTPUTS if name not in {"analysis_spec.json", "setup_source_manifest.json"} and (ROOT / name).exists()]
    if existing:
        raise RuntimeError("formal artifacts already exist; refusing overwrite: " + ", ".join(existing))
    archives, supplements, input_hashes = input_sources()
    active_rows, eligible_codes, universe_audit = frozen_universes()
    setup_manifest = setup_source_manifest()
    if spec != analysis_spec(input_hashes, universe_audit, setup_manifest):
        raise RuntimeError("analysis specification drifted before formal execution")
    active_codes = {row["stock_id"] for row in active_rows}
    active_meta = {row["stock_id"]: row["final_classification"] for row in active_rows}

    data_cfg = replace(SURGE_CFG, maximum_input_date=CFG.maximum_input_date, feature_oos_end=CFG.maximum_input_date)
    raw_stocks, benchmark_bars, load_audit = load_ohlcv(archives, supplement_paths=supplements, cfg=data_cfg)
    prepared, benchmark, prepare_audit = prepare_stocks(raw_stocks, benchmark_bars, data_cfg)
    prepared_by_code = {stock.code: stock for stock in prepared}
    missing = sorted(set(eligible_codes) - set(prepared_by_code))
    if missing:
        raise RuntimeError(f"frozen eligible stocks missing after prepare_stocks: {missing}")
    market = benchmark_context(benchmark)

    observations, structures, pivot_rows, sensitivity_map = [], [], [], {}
    lookahead_violations = 0
    setup_family = {setup_id: family for setup_id, family, _ in SETUP_DEFINITIONS}
    for code in eligible_codes:
        stock = prepared_by_code[code]
        contexts = stock_contexts(stock)
        positions: dict[int, list[str]] = {}
        for position, bar in enumerate(stock.bars):
            if _period_for(bar.date) is None:
                continue
            active_setups = [setup_id for setup_id, flag in setup_flags(stock, position).items() if flag]
            if active_setups:
                positions[position] = active_setups
        primary, pivots = structures_by_signal_index(stock, set(positions), CFG.primary_left, CFG.primary_right)
        sensitivity, sensitivity_pivots = structures_by_signal_index(stock, set(positions), CFG.sensitivity_left, CFG.sensitivity_right)
        for pivot in pivots + sensitivity_pivots:
            pivot_rows.append({**pivot, "pivot_window": f"{pivot['left_sessions']}x{pivot['right_sessions']}", "primary_active_specialist": code in active_codes})
        outcome_cache, period_cache = {}, {}
        for position, setup_ids in positions.items():
            bar = stock.bars[position]
            period, period_end = _period_for(bar.date)
            if (position, period_end) not in outcome_cache:
                outcome_cache[(position, period_end)] = evaluate_outcome(stock, position, period_end)
            outcome = outcome_cache[(position, period_end)]
            structure = primary[position]
            sensitive = sensitivity[position]
            for label in ("a", "b", "c"):
                confirmation = structure.get(f"{label}_confirmation_index")
                if confirmation is not None and confirmation > position:
                    lookahead_violations += 1
            base = {
                "stock_id": code, "stock_name": stock.name, "signal_date": bar.date,
                "period": period, "setup_id": "", "setup_family": "",
                "signal_close": bar.close, "signal_open": bar.open,
                "signal_daily_return": stock.daily_returns[position] if position < len(stock.daily_returns) else None,
                "signal_gap": bar.open / stock.bars[position - 1].close - 1.0 if position else None,
                "signal_volume_ratio20": _volume_ratio(stock, position),
                "market_ma60_context": market.get(bar.date, "UNAVAILABLE"),
                **contexts.get(position, {"stock_er20": None, "stock_er20_quartile": "UNAVAILABLE", "stock_atr20_pct": None, "stock_atr20_percentile_bucket": "UNAVAILABLE"}),
                "specialist_status": active_meta.get(code, "SECONDARY_ELIGIBLE_ONLY"),
                "primary_active_specialist": code in active_codes,
                **structure, "completion_ratio_bucket": ratio_bucket(structure.get("completion_ratio")),
                "primary_group": primary_group(structure.get("completion_ratio")),
                **outcome,
            }
            for setup_id in setup_ids:
                row = {**base, "setup_id": setup_id, "setup_family": setup_family[setup_id]}
                observations.append(row)
                structures.append({key: value for key, value in row.items() if key not in {
                    "signal_open", "signal_daily_return", "signal_gap", "signal_volume_ratio20", "market_ma60_context",
                    "stock_er20", "stock_er20_quartile", "stock_atr20_pct", "stock_atr20_percentile_bucket",
                    "entry_date", "entry_open", "day5_exit_date", "day10_exit_date", "day5_gross_return",
                    "day5_net_return", "day10_gross_return", "day10_net_return", "mfe5", "mae5", "mfe10", "mae10",
                    "up5_within_5d", "up8_within_10d", "up10_within_10d", "up8_before_down5",
                    "up10_before_down5", "downside_first", "calendar_month",
                }})
                sensitivity_map[(code, bar.date, setup_id)] = sensitive

    observations.sort(key=lambda row: (row["signal_date"], row["stock_id"], row["setup_id"]))
    structures.sort(key=lambda row: (row["signal_date"], row["stock_id"], row["setup_id"]))
    pivot_rows.sort(key=lambda row: (row["stock_id"], row["pivot_window"], row["confirmation_index"], row["pivot_index"], row["pivot_type"]))

    scopes = ("PRIMARY_ACTIVE_SPECIALISTS", "SECONDARY_ELIGIBLE_UNIVERSE")
    distribution_rows, bucket_rows, continuous, deciles, coverage_rows = [], [], [], [], []
    period_comparison, family_robustness, stock_robustness = [], [], []
    period_deltas, group_counts = {}, {}
    for scope in scopes:
        for period, _, _ in PERIODS:
            scoped = _scope_rows(observations, active_codes, scope, period)
            for bucket, _, _ in RATIO_BUCKETS:
                selected = [row for row in scoped if row.get("completion_ratio_bucket") == bucket]
                distribution_rows.append({"scope": scope, "period": period, "ratio_bucket": bucket, "count": len(selected), "rate_of_all_signals": len(selected) / len(scoped) if scoped else None})
            available = [row for row in scoped if row.get("structure_status") == "AVAILABLE"]
            coverage_rows.append({
                "scope": scope, "period": period, "dimension": "POOLED", "dimension_value": "ALL",
                "total_frozen_signal_observations": len(scoped), "measured_move_available_count": len(available),
                "coverage_rate": len(available) / len(scoped) if scoped else None,
                "structure_invalid_count": sum(row.get("structure_status") == "STRUCTURE_INVALID" for row in scoped),
                "no_abc_count": sum(row.get("unavailable_reason") == "NO_ABC" for row in scoped),
            })
            for dimension, field in (("STOCK", "stock_id"), ("SETUP_FAMILY", "setup_family")):
                for value in sorted({row[field] for row in scoped}):
                    subset = [row for row in scoped if row[field] == value]
                    coverage_rows.append({
                        "scope": scope, "period": period, "dimension": dimension, "dimension_value": value,
                        "total_frozen_signal_observations": len(subset),
                        "measured_move_available_count": sum(row.get("structure_status") == "AVAILABLE" for row in subset),
                        "coverage_rate": statistics.fmean(row.get("structure_status") == "AVAILABLE" for row in subset),
                        "structure_invalid_count": sum(row.get("structure_status") == "STRUCTURE_INVALID" for row in subset),
                        "no_abc_count": sum(row.get("unavailable_reason") == "NO_ABC" for row in subset),
                    })
            for dimension in ("POOLED", "SETUP_FAMILY", "STOCK", "SETUP_ID"):
                for value, subset in _dimension_groups(scoped, dimension):
                    for bucket, _, _ in RATIO_BUCKETS:
                        picked = [row for row in subset if row.get("completion_ratio_bucket") == bucket]
                        bucket_rows.append({"scope": scope, "period": period, "dimension": dimension, "dimension_value": value, "ratio_bucket": bucket, **summarize(picked)})
            continuous.extend(continuous_rows(scoped, scope, period))
            deciles.extend(decile_rows(scoped, scope, period))
            groups = {name: [row for row in scoped if row.get("primary_group") == name] for name, _, _ in PRIMARY_GROUPS}
            for name, rows in groups.items():
                period_comparison.append({"scope": scope, "period": period, "row_type": "GROUP", "comparison": name, **summarize(rows)})
            for comparison, name in (("A_MINUS_B", "A_R_LT_0_8"), ("C_MINUS_B", "C_R_GE_1_2")):
                deltas = delta_summary(groups[name], groups["B_R_0_8_TO_1_2"])
                period_comparison.append({"scope": scope, "period": period, "row_type": "DELTA", "comparison": comparison, **deltas})
                if scope == "PRIMARY_ACTIVE_SPECIALISTS":
                    period_deltas.setdefault(period, {})[comparison] = deltas
            if scope == "PRIMARY_ACTIVE_SPECIALISTS":
                group_counts[period] = {name: summarize(rows)["n"] for name, rows in groups.items()}
                for family in sorted({row["setup_family"] for row in scoped}):
                    fam = [row for row in scoped if row["setup_family"] == family]
                    fam_groups = {name: [row for row in fam if row.get("primary_group") == name] for name, _, _ in PRIMARY_GROUPS}
                    for name, rows in fam_groups.items():
                        family_robustness.append({"period": period, "setup_family": family, "row_type": "GROUP", "comparison": name, **summarize(rows)})
                    for comparison, name in (("A_MINUS_B", "A_R_LT_0_8"), ("C_MINUS_B", "C_R_GE_1_2")):
                        delta = delta_summary(fam_groups[name], fam_groups["B_R_0_8_TO_1_2"])
                        direction = sum(((delta.get("mean_mfe10_delta") or 0) > 0, (delta.get("day5_net_mean_delta") or 0) > 0, (delta.get("downside_first_rate_delta") or 0) < 0))
                        family_robustness.append({"period": period, "setup_family": family, "row_type": "DELTA", "comparison": comparison, "effect_direction_count": direction, **delta})
                for code in sorted(active_codes):
                    stock_rows = [row for row in scoped if row["stock_id"] == code]
                    stock_groups = {name: [row for row in stock_rows if row.get("primary_group") == name] for name, _, _ in PRIMARY_GROUPS}
                    for comparison, name in (("A_MINUS_B", "A_R_LT_0_8"), ("C_MINUS_B", "C_R_GE_1_2")):
                        stock_robustness.append({
                            "stock_id": code, "stock_name": stock_rows[0]["stock_name"] if stock_rows else "",
                            "period": period, "comparison": comparison,
                            "left_n": summarize(stock_groups[name])["n"], "baseline_n": summarize(stock_groups["B_R_0_8_TO_1_2"])["n"],
                            **delta_summary(stock_groups[name], stock_groups["B_R_0_8_TO_1_2"]),
                        })

    bootstrap_rows = []
    for period, _, _ in PERIODS:
        scoped = _scope_rows(observations, active_codes, "PRIMARY_ACTIVE_SPECIALISTS", period)
        bootstrap_rows.extend(cluster_bootstrap(scoped, period, "signal_date"))
        bootstrap_rows.extend(cluster_bootstrap(scoped, period, "calendar_month"))

    sensitivity_output = []
    for scope in scopes:
        for period, _, _ in PERIODS:
            base_rows = _scope_rows(observations, active_codes, scope, period)
            rows3 = []
            for row in base_rows:
                structure = sensitivity_map[(row["stock_id"], row["signal_date"], row["setup_id"])]
                rows3.append({**row, **structure, "primary_group": primary_group(structure.get("completion_ratio"))})
            groups = {name: [row for row in rows3 if row.get("primary_group") == name] for name, _, _ in PRIMARY_GROUPS}
            for name, values in groups.items():
                sensitivity_output.append({"scope": scope, "period": period, "pivot_window": "3x3", "row_type": "GROUP", "comparison": name, **summarize(values)})
            for comparison, name in (("A_MINUS_B", "A_R_LT_0_8"), ("C_MINUS_B", "C_R_GE_1_2")):
                sensitivity_output.append({"scope": scope, "period": period, "pivot_window": "3x3", "row_type": "DELTA", "comparison": comparison, **delta_summary(groups[name], groups["B_R_0_8_TO_1_2"])})

    primary_all = [row for row in observations if row["stock_id"] in active_codes]
    primary_coverage = statistics.fmean(row["structure_status"] == "AVAILABLE" for row in primary_all) if primary_all else 0.0
    delta_family_rows = [row for row in family_robustness if row["row_type"] == "DELTA"]
    final_classification, rationale = classify(period_deltas, delta_family_rows, primary_coverage, group_counts)
    family_direction = defaultdict(int)
    for row in delta_family_rows:
        if row["comparison"] == "A_MINUS_B":
            family_direction[row["setup_family"]] += row["effect_direction_count"]
    strongest_family = max(sorted(family_direction), key=lambda key: (family_direction[key], key))
    supporting_families = [key for key in sorted(family_direction) if family_direction[key] >= 6]
    family_effect_label = "CROSS_FAMILY_EFFECT" if len(supporting_families) >= 2 else "FAMILY_SPECIFIC_EFFECT" if len(supporting_families) == 1 else "NO_FAMILY_EFFECT"

    lookahead = {
        "study_id": CFG.study_id, "lookahead_violation_count": lookahead_violations,
        "future_pivot_used": False, "same_day_close_execution_used": False,
        "maximum_pivot_confirmation_after_signal": 0,
        "primary_pivot_window": "2x2", "sensitivity_only_pivot_window": "3x3",
    }
    validation = {
        "study_id": CFG.study_id, "status": "COMPLETE",
        "concept_status": "CONCEPT_RECONSTRUCTION_NOT_AUTHOR_STRATEGY",
        "primary_active_stock_count": len(active_codes), "secondary_eligible_stock_count": len(eligible_codes),
        "total_frozen_signal_observations_primary": len(primary_all),
        "total_frozen_signal_observations_secondary": len(observations),
        "primary_measured_move_available_count": sum(row["structure_status"] == "AVAILABLE" for row in primary_all),
        "primary_measured_move_coverage": primary_coverage,
        "primary_unavailable_reasons": dict(sorted(Counter(row["unavailable_reason"] for row in primary_all if row["structure_status"] != "AVAILABLE").items())),
        "final_classification": final_classification, "classification_rationale": rationale,
        "strongest_setup_family": strongest_family, "family_effect_label": family_effect_label,
        "supporting_families": supporting_families,
        "lookahead_violation_count": lookahead_violations,
        "future_outcomes_used_for_structure_construction": False,
        "primary_definition_replaced_by_sensitivity": False,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        "stage_a_refit_count": 0, "model_fit_count": 0,
    }
    if lookahead_violations:
        raise RuntimeError(f"lookahead audit failed: {lookahead_violations}")

    # Row-level evidence is retained for the primary conclusion set.  The full
    # 133-stock secondary universe remains represented in every aggregate
    # robustness table without duplicating >100 MB of raw rows in Git.
    tables = {
        "signal_observations.csv": primary_all,
        "confirmed_pivots.csv": [row for row in pivot_rows if row["primary_active_specialist"]],
        "measured_move_structures.csv": [row for row in structures if row["stock_id"] in active_codes],
        "completion_ratio_distribution.csv": distribution_rows,
        "bucket_performance.csv": bucket_rows, "continuous_relationship.csv": continuous,
        "decile_analysis.csv": deciles, "family_robustness.csv": family_robustness,
        "stock_robustness.csv": stock_robustness, "period_comparison.csv": period_comparison,
        "cluster_bootstrap_summary.csv": bootstrap_rows, "sensitivity_3x3.csv": sensitivity_output,
        "coverage_summary.csv": coverage_rows,
    }
    for name, rows in tables.items():
        write_csv(ROOT / name, rows)
    write_json(ROOT / "lookahead_audit.json", lookahead)
    write_json(ROOT / "validation_summary.json", validation)
    output_hashes = {name: sha256_file(ROOT / name) for name in OUTPUTS if name != "run_manifest.json"}
    manifest = {
        "study_id": CFG.study_id, "status": "COMPLETE",
        "concept_status": "CONCEPT_RECONSTRUCTION_NOT_AUTHOR_STRATEGY",
        "config_fingerprint": CFG.fingerprint(), "input_hashes": input_hashes,
        "universe_source": universe_audit, "setup_source_manifest_sha256": sha256_file(ROOT / "setup_source_manifest.json"),
        "load_audit": load_audit, "prepare_audit": prepare_audit,
        "selection_or_reselection_count": 0, "ratio_threshold_optimization_count": 0,
        "future_pivot_used": False, "same_day_close_execution_used": False,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
        "stage_a_refit_count": 0, "model_fit_count": 0,
        "output_hashes": output_hashes,
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
    }
    manifest["manifest_payload_sha256"] = canonical_hash(manifest)
    write_json(ROOT / "run_manifest.json", manifest)
    return validation


def verify() -> dict:
    manifest = json.loads((ROOT / "run_manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["output_hashes"].items():
        if sha256_file(ROOT / name) != expected:
            raise RuntimeError(f"artifact hash mismatch: {name}")
    for name, expected in manifest["source_hashes"].items():
        if sha256_file(ROOT / name) != expected:
            raise RuntimeError(f"source hash mismatch: {name}")
    payload = {key: value for key, value in manifest.items() if key != "manifest_payload_sha256"}
    if canonical_hash(payload) != manifest["manifest_payload_sha256"]:
        raise RuntimeError("manifest payload hash mismatch")
    return json.loads((ROOT / "validation_summary.json").read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("freeze-spec", "publish", "verify"))
    args = parser.parse_args()
    result = freeze_spec() if args.command == "freeze-spec" else publish() if args.command == "publish" else verify()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
