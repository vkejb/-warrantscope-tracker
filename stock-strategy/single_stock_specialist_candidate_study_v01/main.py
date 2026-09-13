from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from surge_event_study_v01.config import CFG as SURGE_CFG
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .analysis import build_discovery_ranking, calculate_all_metrics, evaluate_stability, select_candidate
from .config import CFG


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01" / "run_manifest.json"
OUTPUTS = (
    "candidate_period_metrics.csv",
    "annual_metrics.csv",
    "discovery_ranking.csv",
    "stability_confirmation.csv",
    "candidate_summary.json",
    "data_audit.json",
    "run_manifest.json",
)
SOURCE_FILES = ("__init__.py", "config.py", "analysis.py", "main.py")


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
    archives, supplements, hashes = [], [], []
    accepted_names = {f"yearly_{year}.zip" for year in range(2019, 2026)} | {"twse_price_supplement.csv"}
    for item in manifest["input_hashes"]:
        raw = Path(item["path"])
        path = raw if raw.is_absolute() else STOCK_STRATEGY / raw
        if path.name not in accepted_names:
            continue
        if not path.is_file():
            raise RuntimeError(f"formal OHLCV input is missing: {path}")
        actual = sha256_file(path)
        if actual != item["sha256"]:
            raise RuntimeError(f"immutable OHLCV input drifted: {path}")
        relative = str(path.relative_to(STOCK_STRATEGY))
        hashes.append({"path": relative, "bytes": path.stat().st_size, "sha256": actual})
        (archives if path.suffix == ".zip" else supplements).append(path)
    if [path.name for path in archives] != [f"yearly_{year}.zip" for year in range(2019, 2026)]:
        raise RuntimeError("expected exact formal yearly 2019-2025 archive set")
    if [path.name for path in supplements] != ["twse_price_supplement.csv"]:
        raise RuntimeError("expected exact formal TWSE supplement")
    return archives, supplements, hashes


def _canonical_hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def publish() -> dict:
    existing = [name for name in OUTPUTS if (ROOT / name).exists()]
    if existing:
        raise RuntimeError("published outputs already exist; refusing overwrite: " + ", ".join(existing))

    archives, supplements, input_hashes = _input_sources()
    data_cfg = replace(SURGE_CFG, maximum_input_date=CFG.maximum_input_date, feature_oos_end=CFG.maximum_input_date)
    stocks, benchmark_bars, load_audit = load_ohlcv(
        archives, supplement_paths=supplements, cfg=data_cfg
    )
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, data_cfg)
    candidates = [stock for stock in prepared if stock.code in CFG.candidates]
    if {stock.code for stock in candidates} != set(CFG.candidates):
        missing = sorted(set(CFG.candidates) - {stock.code for stock in candidates})
        raise RuntimeError("fixed candidates missing from formal OHLCV: " + ", ".join(missing))

    period_metrics, annual_metrics = calculate_all_metrics(candidates, benchmark.calendar, CFG)
    ranking = build_discovery_ranking(period_metrics, annual_metrics, CFG)
    stability = evaluate_stability(period_metrics, CFG)
    selected = select_candidate(ranking, stability)
    cross_period = {
        code: any(
            row["code"] == code and row["cross_period_stability_pass"]
            for row in stability
        )
        for code in CFG.candidates
    }
    summary = {
        "study_id": CFG.study_id,
        "research_type": "SPECIALIST CANDIDATE SCREENING",
        "not_a_trading_strategy": True,
        "future_outcomes_used_in_candidate_score": False,
        "selection_rule": "Highest 2020-2022 discovery rank among candidates passing both fixed later-period stability gates",
        "result_status": (
            "CANDIDATE_SELECTED_FOR_NEXT_STRATEGY_STUDY"
            if selected is not None
            else "NO_CANDIDATE_PASSED_STABILITY_GATE"
        ),
        "selected_candidate": None if selected is None else {
            "code": selected["code"],
            "name": selected["name"],
            "discovery_rank": selected["discovery_rank"],
            "specialist_score": selected["specialist_score"],
        },
        "candidate_stability": [
            {"code": code, "cross_period_stability_pass": cross_period[code], "status": "PASS" if cross_period[code] else "FAIL"}
            for code in CFG.candidates
        ],
        "safety": {
            "actual_orders": CFG.actual_orders,
            "actual_fills": CFG.actual_fills,
            "broker_connections": CFG.broker_connections,
            "model_fit_count": CFG.model_fit_count,
            "stage_a_refit_count": CFG.stage_a_refit_count,
        },
    }
    data_audit = {
        "study_id": CFG.study_id,
        "formal_source_manifest": str(WINNER_MANIFEST.relative_to(STOCK_STRATEGY)),
        "input_hashes_reverified": True,
        "candidate_list_exact": list(CFG.candidates),
        "candidate_rows_loaded": {stock.code: len(stock.bars) for stock in candidates},
        "load_audit": load_audit,
        "prepare_audit": prepare_audit,
        "metric_discontinuity_contract": "Returns, gaps, rolling metrics, drawdowns, and forward paths never cross prepare_stocks segment boundaries",
        "market_calendar_contract": "prepare_stocks union calendar, including existing 0050 suspension handling",
        "corporate_action_status": load_audit["corporate_action_limitation"],
    }

    write_csv(ROOT / "candidate_period_metrics.csv", period_metrics)
    write_csv(ROOT / "annual_metrics.csv", annual_metrics)
    write_csv(ROOT / "discovery_ranking.csv", ranking)
    write_csv(ROOT / "stability_confirmation.csv", stability)
    write_json(ROOT / "candidate_summary.json", summary)
    write_json(ROOT / "data_audit.json", data_audit)

    published = OUTPUTS[:-1]
    manifest = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "result_status": summary["result_status"],
        "formal_publish_count": 1,
        "config": CFG.snapshot(),
        "config_fingerprint": CFG.fingerprint(),
        "period_discipline": {
            "ranking_period": "2020-2022 HISTORICAL_DISCOVERY only",
            "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS; fixed gate only; no reranking, reweighting, or threshold changes",
            "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND; fixed gate only; no reranking, reweighting, or threshold changes",
        },
        "future_outcomes_used_in_candidate_score": False,
        "input_hashes": input_hashes,
        "output_hashes": {name: sha256_file(ROOT / name) for name in published},
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "documentation_sha256": sha256_file(ROOT / "README.md"),
        "safety": summary["safety"],
    }
    manifest["manifest_payload_sha256"] = _canonical_hash(manifest)
    write_json(ROOT / "run_manifest.json", manifest)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Frozen single-stock specialist candidate screening")
    parser.add_argument("command", choices=("publish",))
    args = parser.parse_args()
    if args.command == "publish":
        print(json.dumps(publish(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
