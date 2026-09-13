from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import statistics

from surge_event_study_v01.config import CFG as SURGE_CFG
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .analysis import (
    benchmark_context,
    classify_final,
    discovery_gate,
    evaluate_period,
    market_context_rows,
    month_cluster_bootstrap,
    select_primary_setup,
    summarize_trades,
    tail_robustness,
    validate_cost_contract,
    annual_results,
)
from .config import CFG, PERIODS, SETUP_DEFINITIONS


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
UNIVERSE_PATH = STOCK_STRATEGY / "specialist_universe_discovery_v01" / "final_specialist_universe.csv"
UNIVERSE_MANIFEST = STOCK_STRATEGY / "specialist_universe_discovery_v01" / "run_manifest.json"
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01" / "run_manifest.json"
FROZEN_CODES = (
    "1101", "1216", "1477", "2308", "2324", "2345", "2357", "2376",
    "2379", "2454", "3017", "3653", "3661", "8046", "9958",
)
OUTPUTS = (
    "analysis_spec.json",
    "setup_definitions.json",
    "universe_snapshot.csv",
    "all_setup_discovery_results.csv",
    "discovery_primary_setups.csv",
    "annual_discovery_results.csv",
    "confirmation_2023_2024.csv",
    "stress_2025.csv",
    "primary_setup_bootstrap.csv",
    "primary_setup_tail_robustness.csv",
    "market_context_diagnostics.csv",
    "stock_setup_matrix.csv",
    "final_stock_specific_models.csv",
    "validation_summary.json",
    "run_manifest.json",
)
SOURCE_FILES = ("__init__.py", "config.py", "setups.py", "analysis.py", "main.py")


def write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


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


def _input_sources() -> tuple[list[Path], list[Path], list[dict]]:
    manifest = json.loads(WINNER_MANIFEST.read_text(encoding="utf-8"))
    accepted = {f"yearly_{year}.zip" for year in range(2019, 2026)} | {"twse_price_supplement.csv"}
    archives, supplements, hashes = [], [], []
    for item in manifest["input_hashes"]:
        raw = Path(item["path"])
        path = raw if raw.is_absolute() else STOCK_STRATEGY / raw
        if path.name not in accepted:
            continue
        if not path.is_file():
            raise RuntimeError(f"formal OHLCV input is missing: {path}")
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"immutable OHLCV input drifted: {path}")
        hashes.append({"path": str(path.relative_to(STOCK_STRATEGY)), "bytes": path.stat().st_size, "sha256": actual})
        (archives if path.suffix == ".zip" else supplements).append(path)
    if [path.name for path in archives] != [f"yearly_{year}.zip" for year in range(2019, 2026)]:
        raise RuntimeError("expected exact formal yearly 2019-2025 archive set")
    if [path.name for path in supplements] != ["twse_price_supplement.csv"]:
        raise RuntimeError("expected exact formal TWSE supplement")
    return archives, supplements, hashes


def _frozen_universe() -> tuple[list[dict], dict]:
    manifest = json.loads(UNIVERSE_MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["output_hashes"][UNIVERSE_PATH.name]
    actual = sha256_file(UNIVERSE_PATH)
    if actual != expected:
        raise RuntimeError("frozen Specialist Universe hash drifted")
    with UNIVERSE_PATH.open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row["final_status"] in {"CORE_SPECIALIST", "REGIME_SPECIALIST"}
        ]
    rows.sort(key=lambda row: row["stock_id"])
    if tuple(row["stock_id"] for row in rows) != FROZEN_CODES:
        raise RuntimeError("Specialist Universe is not the exact frozen 15-stock set")
    snapshot = [
        {
            "stock_id": row["stock_id"],
            "stock_name": row["stock_name"],
            "specialist_type": row["final_status"],
            "source_discovery_cluster": row["discovery_cluster"],
            "source_universe_sha256": actual,
        }
        for row in rows
    ]
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return snapshot, {
        "path": str(UNIVERSE_PATH.relative_to(STOCK_STRATEGY)),
        "file_sha256": actual,
        "snapshot_sha256": hashlib.sha256(payload).hexdigest(),
        "stock_count": len(snapshot),
        "source_manifest_sha256": sha256_file(UNIVERSE_MANIFEST),
    }


def _setup_definitions() -> dict:
    return {
        "study_id": CFG.study_id,
        "status": "PREREGISTERED_BEFORE_FORMAL_RESULTS",
        "definitions": [
            {"setup_id": setup_id, "setup_family": family, "definition": definition}
            for setup_id, family, definition in SETUP_DEFINITIONS
        ],
        "short_setups": False,
        "machine_learning": False,
        "warrant_research": False,
    }


def _analysis_spec(input_hashes: list[dict], universe_audit: dict, costs: dict) -> dict:
    return {
        "study_id": CFG.study_id,
        "status": "FROZEN_BEFORE_FORMAL_SETUP_RESULTS",
        "config": CFG.snapshot(),
        "config_fingerprint": CFG.fingerprint(),
        "input_hashes": input_hashes,
        "universe": universe_audit,
        "period_discipline": {
            "setup_selection": "2020-2022 HISTORICAL_DISCOVERY only",
            "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS; frozen primary setup only",
            "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND; frozen primary setup only",
            "later_period_parameter_changes": False,
        },
        "causality": {
            "signal_information": "T regular-session OHLCV and earlier only",
            "entry": "T+1 regular-session Open",
            "primary_exit": "Day5 Close",
            "secondary_exit": "Day10 Close",
            "prior_high_excludes_t": True,
            "volume20": "median T-20 through T-1",
            "ma20_and_ma60": "include T Close; MA20 slope is MA20_T > MA20_T-5",
            "pullback_atr20": "raw-price true-range mean ending T",
            "contraction_atr5_atr20": "raw-price true-range means ending T-1",
            "contraction_range": "(High-Low)/prior Close means ending T-1",
            "complete_outcome_boundary": "Day10 must remain inside the named period and the same prepare_stocks segment",
        },
        "deoverlap": "Per stock and setup variant independently; a new T-close signal is allowed on the prior trade's Day5 close, otherwise active-position signals are ignored",
        "path_diagnostics": "MFE/MAE and +8-before--5 use future session Close returns relative to T+1 Open; descriptive only",
        "cost_assumptions": costs,
        "discovery_gate": {
            "minimum_trades": CFG.minimum_discovery_trades,
            "day5_net_mean": "> 0",
            "day5_net_pf": f"> {CFG.minimum_discovery_day5_net_pf}",
            "annual_consistency": "positive Day5 net mean in at least two of 2020, 2021, 2022",
            "top5_removal": f"Day5 net PF >= {CFG.top5_removed_minimum_net_pf}",
            "quarter_concentration": f"largest positive-quarter share <= {CFG.maximum_positive_quarter_share}",
        },
        "primary_selection_order": [
            "highest minimum annual Day5 net mean",
            "highest overall Day5 net PF",
            "highest Day5 net mean",
            "higher de-overlapped trade count",
            "setup_id lexical ascending",
        ],
        "bootstrap": "5000 calendar-month cluster resamples with replacement; all trades in sampled months retained together",
        "tail_robustness": "Day5 net results at original, remove ceiling(top 1%), remove ceiling(top 5%) winners",
        "market_context": {
            "0050_close_vs_ma60": "normalized 0050 Close_T > inclusive trailing MA60_T",
            "0050_return20_sign": "normalized Close_T/Close_T-20-1 >= 0 versus < 0",
            "entry_gate": False,
            "dependency_label": "MATERIAL_SIGN_REVERSAL only if bucket net means have opposite signs and differ by at least 1 percentage point",
        },
        "stock_setup_matrix": "Within each family, diagnostic variant is selected by discovery Day5 net PF, trade count, then setup_id; later data never chooses the matrix variant",
        "multiple_testing_status": "MULTIPLE_HYPOTHESIS_RESEARCH",
        "future_path_metrics_used_in_selection": False,
        "specialist_type_used_in_selection": False,
        "warrant_research": False,
    }


def _empty_period_row(snapshot: dict, label: str, status: str) -> dict:
    return {
        "stock_id": snapshot["stock_id"],
        "stock_name": snapshot["stock_name"],
        "specialist_type": snapshot["specialist_type"],
        "period": label,
        "primary_setup_id": "",
        "primary_setup_family": "",
        "frozen_setup_available": False,
        "selection_status": status,
        **summarize_trades([]),
    }


def _context_dependency(rows: list[dict]) -> str:
    grouped = {}
    for row in rows:
        if row["day5_net_mean"] is not None:
            grouped.setdefault((row["period"], row["context_dimension"]), []).append(row["day5_net_mean"])
    for values in grouped.values():
        if len(values) >= 2 and min(values) < 0 < max(values) and max(values) - min(values) >= 0.01:
            return "MATERIAL_SIGN_REVERSAL"
    return "NO_MATERIAL_SIGN_REVERSAL"


def _canonical_hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def publish() -> dict:
    existing = [name for name in OUTPUTS if (ROOT / name).exists()]
    if existing:
        raise RuntimeError("published outputs already exist; refusing overwrite: " + ", ".join(existing))
    universe, universe_audit = _frozen_universe()
    archives, supplements, input_hashes = _input_sources()
    costs = validate_cost_contract(CFG)
    write_json(ROOT / "setup_definitions.json", _setup_definitions())
    write_json(ROOT / "analysis_spec.json", _analysis_spec(input_hashes, universe_audit, costs))
    write_csv(ROOT / "universe_snapshot.csv", universe)

    data_cfg = replace(SURGE_CFG, maximum_input_date=CFG.maximum_input_date, feature_oos_end=CFG.maximum_input_date)
    stocks, benchmark_bars, load_audit = load_ohlcv(archives, supplement_paths=supplements, cfg=data_cfg)
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, data_cfg)
    prepared_by_code = {stock.code: stock for stock in prepared}
    missing = sorted(set(FROZEN_CODES) - set(prepared_by_code))
    if missing:
        raise RuntimeError("frozen Specialist Universe stocks missing from OHLCV: " + ", ".join(missing))
    context_by_date = benchmark_context(benchmark)

    summaries_by_period = {}
    trades_by_period = {}
    for label, start, end in PERIODS:
        for snapshot in universe:
            code = snapshot["stock_id"]
            summaries, trades = evaluate_period(prepared_by_code[code], start, end, label, context_by_date, CFG)
            summaries_by_period[(code, label)] = {row["setup_id"]: row for row in summaries}
            trades_by_period[(code, label)] = trades

    discovery_results, annual_rows, primary_rows = [], [], []
    primary_by_code = {}
    for snapshot in universe:
        code = snapshot["stock_id"]
        gated = []
        for setup_id, family, _ in SETUP_DEFINITIONS:
            summary = summaries_by_period[(code, PERIODS[0][0])][setup_id]
            annual = annual_results(code, snapshot["stock_name"], setup_id, trades_by_period[(code, PERIODS[0][0])][setup_id])
            annual_rows.extend(annual)
            row = {**summary, **discovery_gate(summary, trades_by_period[(code, PERIODS[0][0])][setup_id], annual, CFG)}
            discovery_results.append(row)
            gated.append(row)
        primary = select_primary_setup(gated)
        primary_by_code[code] = primary
        too_sparse = all(row["deoverlapped_trade_count"] < CFG.minimum_discovery_trades for row in gated)
        if primary is None:
            primary_rows.append({
                "stock_id": code,
                "stock_name": snapshot["stock_name"],
                "specialist_type": snapshot["specialist_type"],
                "primary_setup_id": "",
                "primary_setup_family": "",
                "selection_status": "TOO_SPARSE" if too_sparse else "NO_DISCOVERY_SETUP_EDGE",
                "discovery_trades": max(row["deoverlapped_trade_count"] for row in gated),
                "discovery_net_mean": None,
                "discovery_net_pf": None,
                "minimum_annual_day5_net_mean": None,
            })
        else:
            primary_rows.append({
                "stock_id": code,
                "stock_name": snapshot["stock_name"],
                "specialist_type": snapshot["specialist_type"],
                "primary_setup_id": primary["setup_id"],
                "primary_setup_family": primary["setup_family"],
                "selection_status": "DISCOVERY_PRIMARY_FROZEN",
                "discovery_trades": primary["deoverlapped_trade_count"],
                "discovery_net_mean": primary["day5_net_mean"],
                "discovery_net_pf": primary["day5_net_pf"],
                "minimum_annual_day5_net_mean": primary["minimum_annual_day5_net_mean"],
            })

    confirmation_rows, stress_rows = [], []
    bootstrap_rows, tail_rows, context_rows, final_rows = [], [], [], []
    final_classification_by_code = {}
    for snapshot in universe:
        code = snapshot["stock_id"]
        primary = primary_by_code[code]
        primary_row = next(row for row in primary_rows if row["stock_id"] == code)
        if primary is None:
            confirmation = _empty_period_row(snapshot, PERIODS[1][0], primary_row["selection_status"])
            stress = _empty_period_row(snapshot, PERIODS[2][0], primary_row["selection_status"])
            confirmation_rows.append(confirmation)
            stress_rows.append(stress)
            classification = classify_final(False, primary_row["selection_status"] == "TOO_SPARSE", None, None)
            dependency = "NO_FROZEN_PRIMARY_SETUP"
        else:
            setup_id = primary["setup_id"]
            later_outputs = []
            stock_context_rows = []
            for label, target in ((PERIODS[1][0], confirmation_rows), (PERIODS[2][0], stress_rows)):
                summary = summaries_by_period[(code, label)][setup_id]
                output = {
                    "stock_id": code,
                    "stock_name": snapshot["stock_name"],
                    "specialist_type": snapshot["specialist_type"],
                    "period": label,
                    "primary_setup_id": setup_id,
                    "primary_setup_family": primary["setup_family"],
                    "frozen_setup_available": True,
                    "selection_status": "FROZEN_FROM_2020_2022",
                    **{key: value for key, value in summary.items() if key not in {"stock_id", "stock_name", "period", "setup_id", "setup_family"}},
                }
                target.append(output)
                later_outputs.append(output)
                trades = trades_by_period[(code, label)][setup_id]
                bootstrap_rows.append({
                    "stock_id": code,
                    "stock_name": snapshot["stock_name"],
                    "specialist_type": snapshot["specialist_type"],
                    "primary_setup_id": setup_id,
                    "primary_setup_family": primary["setup_family"],
                    "period": label,
                    **month_cluster_bootstrap(trades, code, setup_id, label, CFG),
                })
                stock_context_rows.extend(market_context_rows(code, snapshot["stock_name"], setup_id, label, trades))
            for label in (PERIODS[0][0], PERIODS[1][0], PERIODS[2][0]):
                trades = trades_by_period[(code, label)][setup_id]
                for fraction in (0.0, 0.01, 0.05):
                    tail_rows.append({
                        "stock_id": code,
                        "stock_name": snapshot["stock_name"],
                        "specialist_type": snapshot["specialist_type"],
                        "primary_setup_id": setup_id,
                        "primary_setup_family": primary["setup_family"],
                        "period": label,
                        **tail_robustness(trades, fraction),
                    })
            context_rows.extend(stock_context_rows)
            dependency = _context_dependency(stock_context_rows)
            classification = classify_final(True, False, later_outputs[0], later_outputs[1])
        final_classification_by_code[code] = classification
        confirmation = next(row for row in confirmation_rows if row["stock_id"] == code)
        stress = next(row for row in stress_rows if row["stock_id"] == code)
        final_rows.append({
            "stock_id": code,
            "stock_name": snapshot["stock_name"],
            "specialist_type": snapshot["specialist_type"],
            "primary_setup_id": primary_row["primary_setup_id"],
            "primary_setup_family": primary_row["primary_setup_family"],
            "discovery_trades": primary_row["discovery_trades"],
            "discovery_net_mean": primary_row["discovery_net_mean"],
            "discovery_net_pf": primary_row["discovery_net_pf"],
            "confirmation_trades": confirmation["deoverlapped_trade_count"],
            "confirmation_net_mean": confirmation["day5_net_mean"],
            "confirmation_net_pf": confirmation["day5_net_pf"],
            "stress_trades": stress["deoverlapped_trade_count"],
            "stress_net_mean": stress["day5_net_mean"],
            "stress_net_pf": stress["day5_net_pf"],
            "final_classification": classification,
            "market_context_dependency": dependency,
        })

    matrix_rows = []
    families = sorted({family for _, family, _ in SETUP_DEFINITIONS})
    for snapshot in universe:
        code = snapshot["stock_id"]
        matrix = {"stock_id": code, "stock_name": snapshot["stock_name"], "specialist_type": snapshot["specialist_type"]}
        for family in families:
            variants = [row for row in discovery_results if row["stock_id"] == code and row["setup_family"] == family]
            variants.sort(key=lambda row: (-(row["day5_net_pf"] if row["day5_net_pf"] is not None else -1e100), -row["deoverlapped_trade_count"], row["setup_id"]))
            chosen = variants[0]
            setup_id = chosen["setup_id"]
            prefix = family.lower()
            matrix[f"{prefix}_diagnostic_setup_id"] = setup_id
            matrix[f"{prefix}_discovery_net_pf"] = chosen["day5_net_pf"]
            matrix[f"{prefix}_confirmation_net_pf"] = summaries_by_period[(code, PERIODS[1][0])][setup_id]["day5_net_pf"]
            matrix[f"{prefix}_2025_net_pf"] = summaries_by_period[(code, PERIODS[2][0])][setup_id]["day5_net_pf"]
        matrix_rows.append(matrix)

    classifications = {
        label: sorted(code for code, value in final_classification_by_code.items() if value == label)
        for label in (
            "STABLE_STOCK_SPECIFIC_EDGE", "REGIME_DEPENDENT_STOCK_EDGE",
            "DISCOVERY_ONLY_EDGE", "NO_STABLE_STOCK_EDGE", "TOO_SPARSE",
        )
    }
    specialist_comparison = {}
    for specialist_type in ("CORE_SPECIALIST", "REGIME_SPECIALIST"):
        codes = [row["stock_id"] for row in universe if row["specialist_type"] == specialist_type]
        specialist_comparison[specialist_type] = {
            "stock_count": len(codes),
            "discovery_setup_count": sum(primary_by_code[code] is not None for code in codes),
            "stable_edge_count": sum(final_classification_by_code[code] == "STABLE_STOCK_SPECIFIC_EDGE" for code in codes),
            "regime_dependent_edge_count": sum(final_classification_by_code[code] == "REGIME_DEPENDENT_STOCK_EDGE" for code in codes),
        }
    validation = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "multiple_testing_status": "MULTIPLE_HYPOTHESIS_RESEARCH",
        "tested_stock_count": len(universe),
        "tested_variant_count": len(SETUP_DEFINITIONS),
        "total_discovery_hypotheses": len(universe) * len(SETUP_DEFINITIONS),
        "discovery_setup_found_count": sum(primary is not None for primary in primary_by_code.values()),
        "final_classifications": classifications,
        "specialist_type_comparison": specialist_comparison,
        "universe_audit": universe_audit,
        "cost_assumptions": costs,
        "future_path_metrics_used_in_selection": False,
        "later_period_setup_changes": 0,
        "specialist_type_used_in_discovery_selection": False,
        "warrant_research": False,
        "actual_orders": CFG.actual_orders,
        "actual_fills": CFG.actual_fills,
        "broker_connections": CFG.broker_connections,
        "stage_a_refit_count": CFG.stage_a_refit_count,
        "checks": {
            "universe_exactly_frozen_15": tuple(row["stock_id"] for row in universe) == FROZEN_CODES,
            "one_or_zero_primary_per_stock": len(primary_by_code) == len(universe),
            "selection_discovery_only": True,
            "later_setup_frozen": True,
            "same_day_close_execution": False,
            "entry_t_plus_1_open": True,
            "future_metrics_excluded_from_selection": True,
            "no_machine_learning": True,
            "no_warrant_research": True,
            "safety_zero": CFG.actual_orders == CFG.actual_fills == CFG.broker_connections == CFG.stage_a_refit_count == 0,
        },
    }

    write_csv(ROOT / "all_setup_discovery_results.csv", discovery_results)
    write_csv(ROOT / "discovery_primary_setups.csv", primary_rows)
    write_csv(ROOT / "annual_discovery_results.csv", annual_rows)
    write_csv(ROOT / "confirmation_2023_2024.csv", confirmation_rows)
    write_csv(ROOT / "stress_2025.csv", stress_rows)
    if bootstrap_rows:
        write_csv(ROOT / "primary_setup_bootstrap.csv", bootstrap_rows)
        write_csv(ROOT / "primary_setup_tail_robustness.csv", tail_rows)
        write_csv(ROOT / "market_context_diagnostics.csv", context_rows)
    else:
        placeholder = [{"status": "NO_DISCOVERY_PRIMARY_SETUPS"}]
        write_csv(ROOT / "primary_setup_bootstrap.csv", placeholder)
        write_csv(ROOT / "primary_setup_tail_robustness.csv", placeholder)
        write_csv(ROOT / "market_context_diagnostics.csv", placeholder)
    write_csv(ROOT / "stock_setup_matrix.csv", matrix_rows)
    write_csv(ROOT / "final_stock_specific_models.csv", final_rows)
    write_json(ROOT / "validation_summary.json", validation)

    manifest = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "formal_publish_count": 1,
        "config_fingerprint": CFG.fingerprint(),
        "universe_hash": universe_audit,
        "input_hashes": input_hashes,
        "cost_assumptions": costs,
        "output_hashes": {name: sha256_file(ROOT / name) for name in OUTPUTS[:-1]},
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "documentation_sha256": sha256_file(ROOT / "README.md"),
        "load_audit": load_audit,
        "prepare_audit": prepare_audit,
        "future_path_metrics_used_in_selection": False,
        "multiple_testing_status": "MULTIPLE_HYPOTHESIS_RESEARCH",
        "warrant_research": False,
        "safety": {
            "actual_orders": CFG.actual_orders,
            "actual_fills": CFG.actual_fills,
            "broker_connections": CFG.broker_connections,
            "stage_a_refit_count": CFG.stage_a_refit_count,
        },
    }
    manifest["manifest_payload_sha256"] = _canonical_hash(manifest)
    write_json(ROOT / "run_manifest.json", manifest)
    return validation


def main() -> None:
    parser = argparse.ArgumentParser(description="Frozen per-stock interpretable daily setup discovery")
    parser.add_argument("command", choices=("publish",))
    args = parser.parse_args()
    if args.command == "publish":
        print(json.dumps(publish(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
