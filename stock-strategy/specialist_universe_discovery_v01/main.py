from __future__ import annotations

import argparse
import csv
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from surge_event_study_v01.config import CFG as SURGE_CFG
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .analysis import (
    DISCOVERY_LABEL,
    LATER_LABELS,
    add_discovery_features,
    benchmark_returns,
    calculate_metrics,
    classify_stability,
    cluster_discovery,
    correlation_matrix,
    discovery_eligibility,
    final_status,
    quality_scores,
    select_representatives,
)
from .config import CFG, CLUSTER_FEATURES, SCORE_WEIGHTS


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01" / "run_manifest.json"
OUTPUTS = (
    "analysis_spec.json",
    "eligible_universe.csv",
    "discovery_behavior_features.csv",
    "discovery_quality_scores.csv",
    "behavior_clusters.csv",
    "cluster_centroids.csv",
    "cluster_representatives.csv",
    "discovery_selected_pool.csv",
    "discovery_correlation_matrix.csv",
    "annual_diagnostics.csv",
    "later_period_stability.csv",
    "final_specialist_universe.csv",
    "validation_summary.json",
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
    expected_archives = [f"yearly_{year}.zip" for year in range(2019, 2026)]
    if [path.name for path in archives] != expected_archives:
        raise RuntimeError("expected exact formal yearly 2019-2025 archive set")
    if [path.name for path in supplements] != ["twse_price_supplement.csv"]:
        raise RuntimeError("expected exact formal TWSE supplement")
    return archives, supplements, hashes


def _analysis_spec(input_hashes: list[dict]) -> dict:
    return {
        "status": "FROZEN_BEFORE_FORMAL_RANKING",
        "study_id": CFG.study_id,
        "config_fingerprint": CFG.fingerprint(),
        "config": CFG.snapshot(),
        "input_hashes": input_hashes,
        "period_discipline": {
            "eligibility_normalization_score_cluster_representatives_and_ordering": "2020-2022 HISTORICAL_DISCOVERY only",
            "2023_2024": "RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS; stability and classification only",
            "2025": "STRESS_PREVALENCE_SEEN_NOT_BLIND; stability and classification only",
            "later_period_reclustering": False,
            "later_period_reranking": False,
        },
        "market_sensitivity_definition": {
            "daily_alignment": "same-date continuous stock and continuous normalized 0050 close returns",
            "beta_0050": "OLS slope with intercept: stock daily return on 0050 daily return",
            "correlation_0050": "Pearson daily return correlation",
            "upside_capture": "mean stock return / mean 0050 return on sessions where 0050 return > 0",
            "downside_capture": "mean stock return / mean 0050 return on sessions where 0050 return < 0",
            "idiosyncratic_volatility": "population standard deviation of OLS residuals times sqrt(252)",
        },
        "quality_score_definition": {
            "weights": SCORE_WEIGHTS,
            "liquidity": "eligible-universe percentile of discovery median turnover",
            "continuity": "eligible-universe percentile of discovery coverage",
            "behavior_stability": "inverse percentile of equal mean annual CV for ATR14, absolute return, and turnover in 2020/2021/2022",
            "tradable_volatility": "equal mean of ATR14 and absolute-return percentile credits; each credit is percentile/0.60 capped at 1.0 so the most volatile tail cannot outrank merely by magnitude",
            "gap_safety": "inverse percentile of discovery P95 absolute overnight gap",
            "trend_structure_quality": "equal mean of ER20 percentile/0.60 capped at 1.0 and inverse percentile of absolute lag1 autocorrelation",
        },
        "clustering_definition": {
            "features": list(CLUSTER_FEATURES),
            "winsorization": "discovery eligible cross-section 2.5%/97.5% per feature",
            "standardization": "population z-score of discovery winsorized features",
            "algorithm": "deterministic farthest-first initialized Lloyd KMeans; lowest stock_id is first centroid; argmin cluster tie and stock-order ties",
            "k": 8,
            "future_data_used": False,
            "liquidity_feature_used": False,
        },
        "representative_selection_definition": {
            "primary": "highest discovery quality score in each non-empty cluster; stock_id ascending tie-break",
            "secondary": "cluster rank2 only when discovery daily return correlation with primary is below 0.75",
            "ordering": "all cluster primaries by cluster id, then eligible secondaries by score descending, average correlation to already-selected pool ascending, stock_id ascending",
            "maximum_per_cluster": 2,
            "maximum_pool_size": 15,
            "minimum_pool_size_is_not_forced": True,
        },
        "stability_definition": {
            "core": "both later periods retain normal coverage/liquidity and pass ATR 0.60-1.80, absolute return 0.60-1.80, P95 gap <=2x, beta absolute change <=0.50",
            "regime": "severe tradability passes but one or more normal/core behavior checks fail",
            "exclude": "any later period fails fixed severe coverage, sessions, liquidity, missing-run, or extreme-gap checks",
            "volatility_or_beta_shift_alone_causes_exclusion": False,
        },
        "future_outcomes_used_for_universe_selection": False,
        "industry_status": "UNAVAILABLE_NO_RELIABLE_REPOSITORY_SOURCE",
        "industry_used_for_score_cluster_or_selection": False,
    }


def _canonical_hash(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def publish() -> dict:
    existing = [name for name in OUTPUTS if (ROOT / name).exists()]
    if existing:
        raise RuntimeError("published outputs already exist; refusing overwrite: " + ", ".join(existing))
    archives, supplements, input_hashes = _input_sources()
    write_json(ROOT / "analysis_spec.json", _analysis_spec(input_hashes))

    data_cfg = replace(SURGE_CFG, maximum_input_date=CFG.maximum_input_date, feature_oos_end=CFG.maximum_input_date)
    stocks, benchmark_bars, load_audit = load_ohlcv(archives, supplement_paths=supplements, cfg=data_cfg)
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, data_cfg)
    benchmark_by_date = benchmark_returns(benchmark)
    eligibility, eligible_basic, prepared_by_code = discovery_eligibility(stocks, prepared, benchmark.calendar, benchmark_by_date, CFG)
    if len(eligible_basic) < CFG.cluster_count:
        raise RuntimeError(f"eligible universe too small for fixed K=8: {len(eligible_basic)}; thresholds will not be relaxed")
    features = add_discovery_features(eligible_basic, prepared_by_code, benchmark.calendar, benchmark_by_date, CFG)
    scores = quality_scores(features, CFG)
    assignments, centroids, cluster_audit = cluster_discovery(features, CFG)
    representatives, selected_scores = select_representatives(scores, assignments, prepared_by_code, CFG)

    feature_by_code = {row["stock_id"]: row for row in features}
    score_by_code = {row["stock_id"]: row for row in scores}
    assignment_by_code = {row["stock_id"]: row for row in assignments}
    label_by_cluster = {row["discovery_cluster"]: row["cluster_descriptive_label"] for row in centroids}
    cluster_rank = {}
    for cluster in range(CFG.cluster_count):
        members = [row for row in scores if assignment_by_code[row["stock_id"]]["discovery_cluster"] == cluster]
        members.sort(key=lambda row: (-row["discovery_quality_score"], row["stock_id"]))
        cluster_rank.update({row["stock_id"]: rank for rank, row in enumerate(members, 1)})
    behavior_rows = [
        {
            **assignment_by_code[code],
            "cluster_descriptive_label": label_by_cluster[assignment_by_code[code]["discovery_cluster"]],
            "discovery_rank_within_cluster": cluster_rank[code],
            "discovery_quality_score": score_by_code[code]["discovery_quality_score"],
        }
        for code in sorted(feature_by_code)
    ]
    selected_codes = [row["stock_id"] for row in selected_scores]
    matrix_rows, correlation_audit = correlation_matrix(selected_codes, prepared_by_code, CFG)
    representative_by_code = {row["stock_id"]: row for row in representatives}
    selected_pool = []
    for order, code in enumerate(selected_codes, 1):
        feature = feature_by_code[code]
        cluster = assignment_by_code[code]["discovery_cluster"]
        selected_pool.append({
            "discovery_selection_order": order,
            "stock_id": code,
            "stock_name": feature["stock_name"],
            "discovery_cluster": cluster,
            "cluster_descriptive_label": label_by_cluster[cluster],
            "discovery_rank_within_cluster": cluster_rank[code],
            "discovery_quality_score": score_by_code[code]["discovery_quality_score"],
            "average_correlation_to_pool": next(row["average_correlation_to_pool"] for row in matrix_rows if row["stock_id"] == code),
            **{field: feature[field] for field in CLUSTER_FEATURES},
            "median_daily_turnover_proxy": feature["median_daily_turnover_proxy"],
            "p95_abs_overnight_gap_pct": feature["p95_abs_overnight_gap_pct"],
            "median_mfe10_pct_descriptive_only": feature["median_mfe10_pct"],
            "median_mae10_pct_descriptive_only": feature["median_mae10_pct"],
            "plus_8pct_within_10d_close_hit_rate_descriptive_only": feature["plus_8pct_within_10d_close_hit_rate"],
            "minus_5pct_within_10d_close_hit_rate_descriptive_only": feature["minus_5pct_within_10d_close_hit_rate"],
            "future_outcomes_used_for_universe_selection": False,
        })

    annual_rows, later_metrics, stability_rows = [], [], []
    for code in selected_codes:
        stock = prepared_by_code[code]
        for year in range(2020, 2026):
            annual_rows.append(calculate_metrics(stock, benchmark.calendar, benchmark_by_date, f"{year}0101", f"{year}1231", str(year), include_forward=True, cfg=CFG))
        discovery = feature_by_code[code]
        for label, start, end in (
            (LATER_LABELS[0], "20230101", "20241231"),
            (LATER_LABELS[1], "20250101", "20251231"),
        ):
            later = calculate_metrics(stock, benchmark.calendar, benchmark_by_date, start, end, label, cfg=CFG)
            later_metrics.append(later)
            stability_rows.append(classify_stability(discovery, later, CFG))

    final_status_by_code = {
        code: final_status([row for row in stability_rows if row["stock_id"] == code])
        for code in selected_codes
    }
    stability_by_key = {(row["stock_id"], row["period"]): row for row in stability_rows}
    final_rows = []
    for code in sorted(feature_by_code):
        feature = feature_by_code[code]
        cluster = assignment_by_code[code]["discovery_cluster"]
        selected = code in selected_codes
        final_rows.append({
            "stock_id": code,
            "stock_name": feature["stock_name"],
            "discovery_cluster": cluster,
            "cluster_descriptive_label": label_by_cluster[cluster],
            "discovery_quality_score": score_by_code[code]["discovery_quality_score"],
            "discovery_rank_within_cluster": cluster_rank[code],
            "discovery_beta_0050": feature["beta_0050"],
            "discovery_corr_0050": feature["correlation_0050"],
            "discovery_downside_capture": feature["downside_capture"],
            "discovery_ATR14": feature["median_atr14_pct"],
            "discovery_ER20": feature["median_efficiency20"],
            "discovery_idiosyncratic_vol": feature["idiosyncratic_volatility_annualized_pct"],
            "discovery_P95_gap": feature["p95_abs_overnight_gap_pct"],
            "discovery_turnover": feature["median_daily_turnover_proxy"],
            "discovery_selected": selected,
            "2023_24_stability": stability_by_key[(code, LATER_LABELS[0])]["period_stability"] if selected else "",
            "2025_stability": stability_by_key[(code, LATER_LABELS[1])]["period_stability"] if selected else "",
            "final_status": final_status_by_code[code] if selected else "NOT_SELECTED",
            "current_industry_descriptive": "",
            "industry_label_status": "UNAVAILABLE_NO_RELIABLE_REPOSITORY_SOURCE",
        })

    defensive_codes = sorted(row["stock_id"] for row in features if row["beta_0050"] <= CFG.defensive_beta_maximum and row["downside_capture"] <= CFG.defensive_downside_capture_maximum)
    selected_defensive = sorted(set(defensive_codes) & set(selected_codes))
    statuses = {status: sorted(code for code, value in final_status_by_code.items() if value == status) for status in ("CORE_SPECIALIST", "REGIME_SPECIALIST", "EXCLUDED_AFTER_STABILITY")}
    selected_cluster_counts = {
        str(cluster): sum(assignment_by_code[code]["discovery_cluster"] == cluster for code in selected_codes)
        for cluster in range(CFG.cluster_count)
    }
    validation = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "raw_ordinary_stock_count": load_audit["ordinary_code_count"],
        "prepared_stock_count": prepare_audit["prepared_stock_count"],
        "eligible_stock_count": len(features),
        "discovery_selected_count": len(selected_codes),
        "final_specialist_universe_count": len(statuses["CORE_SPECIALIST"]) + len(statuses["REGIME_SPECIALIST"]),
        "selected_codes": selected_codes,
        "final_status_codes": statuses,
        "defensive_candidate_count": len(defensive_codes),
        "defensive_candidate_codes": defensive_codes,
        "selected_defensive_count": len(selected_defensive),
        "selected_defensive_codes": selected_defensive,
        "defensive_status": "NO_DEFENSIVE_SPECIALIST_FOUND" if not defensive_codes else "DEFENSIVE_SPECIALISTS_FOUND",
        "correlation_audit": correlation_audit,
        "concentration_audit": {
            "selected_count_by_discovery_cluster": selected_cluster_counts,
            "maximum_selected_from_one_cluster": max(selected_cluster_counts.values()),
            "maximum_cluster_share": max(selected_cluster_counts.values()) / len(selected_codes),
            "cluster_overconcentration": max(selected_cluster_counts.values()) > CFG.maximum_representatives_per_cluster,
            "technology_concentration_status": "NOT_ASSESSABLE_WITHOUT_RELIABLE_INDUSTRY_LABELS",
        },
        "industry_status": "UNAVAILABLE_NO_RELIABLE_REPOSITORY_SOURCE",
        "industry_used_for_score_cluster_or_selection": False,
        "future_outcomes_used_for_universe_selection": False,
        "later_period_reclustering_count": 0,
        "later_period_reranking_count": 0,
        "stage_a_refit_count": CFG.stage_a_refit_count,
        "actual_orders": CFG.actual_orders,
        "actual_fills": CFG.actual_fills,
        "broker_connections": CFG.broker_connections,
        "checks": {
            "fixed_k_equals_8": CFG.cluster_count == 8,
            "all_clusters_nonempty": all(row["stock_count"] > 0 for row in centroids),
            "maximum_two_representatives_per_cluster": all(value <= 2 for value in selected_cluster_counts.values()),
            "pool_maximum_15": len(selected_codes) <= CFG.final_pool_maximum,
            "future_outcomes_excluded": True,
            "industry_excluded": True,
            "later_periods_diagnostic_only": True,
        },
    }

    write_csv(ROOT / "eligible_universe.csv", eligibility)
    write_csv(ROOT / "discovery_behavior_features.csv", features)
    write_csv(ROOT / "discovery_quality_scores.csv", scores)
    write_csv(ROOT / "behavior_clusters.csv", behavior_rows)
    write_csv(ROOT / "cluster_centroids.csv", centroids)
    write_csv(ROOT / "cluster_representatives.csv", representatives)
    write_csv(ROOT / "discovery_selected_pool.csv", selected_pool)
    write_csv(ROOT / "discovery_correlation_matrix.csv", matrix_rows)
    write_csv(ROOT / "annual_diagnostics.csv", annual_rows)
    write_csv(ROOT / "later_period_stability.csv", stability_rows)
    write_csv(ROOT / "final_specialist_universe.csv", final_rows)
    write_json(ROOT / "validation_summary.json", validation)

    published = OUTPUTS[:-1]
    manifest = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "formal_publish_count": 1,
        "config_fingerprint": CFG.fingerprint(),
        "input_hashes": input_hashes,
        "output_hashes": {name: sha256_file(ROOT / name) for name in published},
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "documentation_sha256": sha256_file(ROOT / "README.md"),
        "load_audit": load_audit,
        "prepare_audit": prepare_audit,
        "cluster_audit": cluster_audit,
        "future_outcomes_used_for_universe_selection": False,
        "industry_used_for_score_cluster_or_selection": False,
        "later_period_reclustering_count": 0,
        "later_period_reranking_count": 0,
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
    parser = argparse.ArgumentParser(description="Frozen discovery-only specialist universe research")
    parser.add_argument("command", choices=("publish",))
    args = parser.parse_args()
    if args.command == "publish":
        print(json.dumps(publish(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
