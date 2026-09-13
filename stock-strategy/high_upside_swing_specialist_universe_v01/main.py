from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from surge_event_study_v01.config import CFG as SURGE_CFG
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .analysis import (
    correlation_matrix,
    daily_metrics,
    eligibility,
    final_classification,
    greedy_select,
    later_assessment,
    median,
    per_stock_pool_correlations,
    quantile,
    repeatability,
    score_discovery,
    swing_metrics,
    tail_dependent,
    tail_diagnostics,
)
from .config import CFG, PERIODS, SCORE_WEIGHTS, THRESHOLDS


ROOT = Path(__file__).resolve().parent
STOCK_STRATEGY = ROOT.parent
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01" / "run_manifest.json"
OLD_UNIVERSE = STOCK_STRATEGY / "specialist_universe_discovery_v01" / "final_specialist_universe.csv"
OLD_MANIFEST = STOCK_STRATEGY / "specialist_universe_discovery_v01" / "run_manifest.json"
OUTPUTS = (
    "analysis_spec.json",
    "eligibility_universe.csv",
    "daily_movement_metrics.csv",
    "swing_opportunity_metrics.csv",
    "annual_swing_repeatability.csv",
    "deoverlapped_swing_episodes.csv",
    "swing_persistence_metrics.csv",
    "discovery_swing_scores.csv",
    "discovery_rankings.csv",
    "correlation_matrix.csv",
    "primary_specialist_pool.csv",
    "high_correlation_reserve.csv",
    "high_upside_swing_reserve.csv",
    "confirmation_2023_2024.csv",
    "stress_2025.csv",
    "final_high_upside_specialist_universe.csv",
    "comparison_vs_stable_specialist_universe.csv",
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
        if not path.is_file():
            raise RuntimeError(f"formal OHLCV input missing: {path}")
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


def old_universe() -> tuple[list[str], dict]:
    manifest = json.loads(OLD_MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["output_hashes"][OLD_UNIVERSE.name]
    actual = sha256_file(OLD_UNIVERSE)
    if actual != expected:
        raise RuntimeError("previous stable Specialist Universe hash drifted")
    with OLD_UNIVERSE.open(encoding="utf-8-sig", newline="") as handle:
        codes = sorted(
            row["stock_id"] for row in csv.DictReader(handle)
            if row["final_status"] in {"CORE_SPECIALIST", "REGIME_SPECIALIST"}
        )
    if len(codes) != 15:
        raise RuntimeError("previous stable Specialist Universe is not the published 15-stock set")
    return codes, {"path": str(OLD_UNIVERSE.relative_to(STOCK_STRATEGY)), "sha256": actual, "stock_count": len(codes)}


def analysis_spec(input_hashes: list[dict], old_audit: dict) -> dict:
    return {
        "study_id": CFG.study_id,
        "status": "FROZEN_BEFORE_FORMAL_RESULTS",
        "config": CFG.snapshot(),
        "config_fingerprint": CFG.fingerprint(),
        "input_hashes": input_hashes,
        "previous_universe": old_audit,
        "research_periods": {
            "selection_period": "2020-2022",
            "confirmation_period": "2023-2024 RETROSPECTIVE_CONFIRMATION_NOT_BLIND_OOS",
            "stress_period": "2025 STRESS_PREVALENCE_SEEN_NOT_BLIND",
        },
        "forward_characterization": {
            "reference_entry": "T+1 regular-session Open",
            "path": "Day1-Day10 session Close returns",
            "complete_path_required": "same prepare_stocks segment, consecutive market sessions, and entirely inside named period",
            "future_outcomes_used_for_discovery_universe_characterization": True,
            "later_period_outcomes_used_for_discovery_selection": False,
        },
        "daily_movement_gate": "median ATR14 >=2.0% OR median (High-Low)/prior Close >=2.5%; minimum gate only",
        "episode_definition": {
            "thresholds": [list(item) for item in THRESHOLDS],
            "start": "each evaluable T whose future Close path reaches threshold",
            "cooldown": "after accepted T, calendar sessions T+1 through T+10 cannot start another episode for the same stock and threshold; T+11 may start",
            "ranking_measure": "deoverlapped episodes per 252 period sessions",
        },
        "swing_persistence_definition": "median absolute Day10 Close return from T+1 Open divided by sqrt(10) times discovery median daily (High-Low)/prior Close",
        "tail_dependency_definition": "diagnostic only; true if Top5%-MFE removal leaves P75 MFE10 below 80% of original or +10 raw hit rate below 75% of original",
        "repeatability_gate": "at least two of 2020, 2021, 2022 each have >=3 deoverlapped +8 episodes and >=2 deoverlapped +10 episodes",
        "score_definition": {
            "weights": SCORE_WEIGHTS,
            "normalization": "tie-aware 0-1 percentile ranks over the 2020-2022 eligible cross-section only",
            "daily_movement_component": "equal mean of median ATR14 and median daily-range percentile credits",
            "mae_used": False,
            "downside_first_used": False,
        },
        "primary_selection": {
            "order": "repeatability-passed stocks by Swing Score descending, stock_id ascending",
            "greedy_correlation": "accept only when discovery daily-return correlation with every selected stock <=0.80",
            "maximum": 15,
            "cluster_quota": False,
            "minimum_is_not_forced": True,
        },
        "later_classification": {
            "persistent": "both later periods: +8 episode retention >=60%, +10 >=50%, median MFE10 >=60%, and normal liquidity",
            "regime": "tradability retained and exactly one period passes, or any later upside metric expands to >=120% of discovery",
            "discovery_only": "both later periods fail and no upside metric expands to >=120% of discovery",
            "lost_tradability": "any later period fails fixed severe coverage, sessions, turnover, missing-run, or gap checks",
        },
        "industry_used_for_selection": False,
        "setup_results_used_for_selection": False,
        "model_fit_count": 0,
        "stage_a_refit_count": 0,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }


def freeze_spec() -> dict:
    archives, supplements, hashes = input_sources()
    del archives, supplements
    _, old_audit = old_universe()
    payload = analysis_spec(hashes, old_audit)
    path = ROOT / "analysis_spec.json"
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != payload:
            raise RuntimeError("existing analysis_spec.json does not match current frozen specification")
        return payload
    write_json(path, payload)
    return payload


def aggregate_universe(
    label: str, codes: list[str], metrics_by_code: dict[str, dict], correlation_audit: dict,
) -> dict:
    rows = [metrics_by_code[code] for code in codes]
    return {
        "row_type": "UNIVERSE",
        "universe": label,
        "stock_count": len(codes),
        "median_atr14_pct": median(row["median_atr14_pct"] for row in rows),
        "median_daily_range_pct": median(row["median_daily_range_pct"] for row in rows),
        "median_up8_episode_rate_per_252": median(row["up8_episodes_per_252_sessions"] for row in rows),
        "median_up10_episode_rate_per_252": median(row["up10_episodes_per_252_sessions"] for row in rows),
        "median_up15_episode_rate_per_252": median(row["up15_episodes_per_252_sessions"] for row in rows),
        "median_mfe10": median(row["median_mfe10"] for row in rows),
        "median_p75_mfe10": median(row["p75_mfe10"] for row in rows),
        "median_er20": median(row["median_er20"] for row in rows),
        "average_pairwise_correlation": correlation_audit["average_pairwise_correlation"],
    }


def publish() -> dict:
    spec = freeze_spec()
    existing = [name for name in OUTPUTS if name != "analysis_spec.json" and (ROOT / name).exists()]
    if existing:
        raise RuntimeError("published outputs already exist; refusing overwrite: " + ", ".join(existing))
    archives, supplements, input_hashes = input_sources()
    old_codes, old_audit = old_universe()
    if spec != analysis_spec(input_hashes, old_audit):
        raise RuntimeError("frozen analysis specification drifted before formal run")

    data_cfg = replace(SURGE_CFG, maximum_input_date=CFG.maximum_input_date, feature_oos_end=CFG.maximum_input_date)
    stocks, benchmark_bars, load_audit = load_ohlcv(archives, supplement_paths=supplements, cfg=data_cfg)
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_bars, data_cfg)
    eligibility_rows, discovery_daily, prepared_by_code = eligibility(stocks, prepared, benchmark.calendar, CFG)
    eligible_codes = [row["stock_id"] for row in eligibility_rows if row["eligible"]]
    if not eligible_codes:
        raise RuntimeError("no stocks passed fixed eligibility; thresholds will not be relaxed")

    daily_rows, swing_rows, persistence_rows, annual_rows, episode_rows = [], [], [], [], []
    discovery_combined = {}
    for code in eligible_codes:
        stock = prepared_by_code[code]
        daily = discovery_daily[code]
        swing, episodes, outcomes = swing_metrics(
            stock, daily, PERIODS[0][1], PERIODS[0][2], PERIODS[0][0], PERIODS[0][3], CFG
        )
        top1, top5 = tail_diagnostics(outcomes, 0.01), tail_diagnostics(outcomes, 0.05)
        original = {"p75_mfe10": swing["p75_mfe10"], "up10_raw_hit_rate": swing["up10_raw_hit_rate"]}
        swing.update({
            "top1_removed_observations": top1["removed_observations"],
            "top1_median_mfe10": top1["median_mfe10"],
            "top1_p75_mfe10": top1["p75_mfe10"],
            "top1_up8_raw_hit_rate": top1["up8_raw_hit_rate"],
            "top1_up10_raw_hit_rate": top1["up10_raw_hit_rate"],
            "top5_removed_observations": top5["removed_observations"],
            "top5_median_mfe10": top5["median_mfe10"],
            "top5_p75_mfe10": top5["p75_mfe10"],
            "top5_up8_raw_hit_rate": top5["up8_raw_hit_rate"],
            "top5_up10_raw_hit_rate": top5["up10_raw_hit_rate"],
            "tail_dependent_swing_profile": tail_dependent(original, top5, CFG),
        })
        combined = {**daily, **{key: value for key, value in swing.items() if key not in {"period", "stock_id", "stock_name"}}}
        discovery_combined[code] = combined
        daily_rows.append(daily)
        swing_rows.append(swing)
        persistence_rows.append({
            "stock_id": code, "stock_name": stock.name, "period": PERIODS[0][0],
            "median_er10": daily["median_er10"], "median_er20": daily["median_er20"], "p75_er20": daily["p75_er20"],
            "median_abs_day5_close_displacement": swing["median_abs_day5_close_displacement"],
            "median_abs_day10_close_displacement": swing["median_abs_day10_close_displacement"],
            "median_daily_range_pct": daily["median_daily_range_pct"], "swing_persistence": swing["swing_persistence"],
        })
        episode_rows.extend(episodes)
        for label, start, end, years in ((str(year), f"{year}0101", f"{year}1231", 1) for year in (2020, 2021, 2022)):
            annual_daily = daily_metrics(stock, benchmark.calendar, start, end, label, CFG)
            annual_swing, annual_episodes, _ = swing_metrics(stock, annual_daily, start, end, label, years, CFG)
            row = {**annual_daily, **{key: value for key, value in annual_swing.items() if key not in {"period", "stock_id", "stock_name"}}}
            annual_rows.append(row)
            episode_rows.extend(annual_episodes)

    repeatability_by_code = repeatability(annual_rows, CFG)
    for row in annual_rows:
        row.update(repeatability_by_code[row["stock_id"]])
    score_input = [{**discovery_combined[code], **repeatability_by_code[code]} for code in eligible_codes]
    scored = score_discovery(score_input, CFG)
    rankings = []
    for row in scored:
        if not row["swing_repeatability_pass"]:
            continue
        rankings.append({**row, "discovery_rank": len(rankings) + 1})
    if not rankings:
        raise RuntimeError("no stock passed fixed Swing Repeatability Gate; thresholds will not be relaxed")
    selected, high_corr, reserve = greedy_select(rankings, prepared_by_code, CFG)
    selected_codes = [row["stock_id"] for row in selected]
    matrix_rows, pool_corr_audit = correlation_matrix(selected_codes, prepared_by_code)
    pool_corr_by_code = per_stock_pool_correlations(matrix_rows, selected_codes)

    confirmation_rows, stress_rows, later_by_code, all_later_episodes = [], [], defaultdict(list), []
    for selected_row in selected:
        code = selected_row["stock_id"]
        stock = prepared_by_code[code]
        discovery = discovery_combined[code]
        for label, start, end, years in PERIODS[1:]:
            later_daily = daily_metrics(stock, benchmark.calendar, start, end, label, CFG)
            later_swing, later_episodes, _ = swing_metrics(stock, later_daily, start, end, label, years, CFG)
            assessed = later_assessment(discovery, later_daily, later_swing, CFG)
            later_by_code[code].append(assessed)
            all_later_episodes.extend(later_episodes)
            (confirmation_rows if label == PERIODS[1][0] else stress_rows).append(assessed)
    episode_rows.extend(all_later_episodes)

    final_rows = []
    for row in selected:
        code = row["stock_id"]
        later = later_by_code[code]
        confirmation = next(item for item in later if item["period"] == PERIODS[1][0])
        stress = next(item for item in later if item["period"] == PERIODS[2][0])
        final_rows.append({
            "stock_id": code,
            "stock_name": row["stock_name"],
            "discovery_rank": row["discovery_rank"],
            "swing_score": row["swing_score"],
            "median_ATR14": row["median_atr14_pct"],
            "median_daily_range": row["median_daily_range_pct"],
            "up5_5d_raw_rate": row["up5_raw_hit_rate"],
            "up8_10d_raw_rate": row["up8_raw_hit_rate"],
            "up8_episode_rate": row["up8_episodes_per_252_sessions"],
            "up10_10d_raw_rate": row["up10_raw_hit_rate"],
            "up10_episode_rate": row["up10_episodes_per_252_sessions"],
            "up15_10d_raw_rate": row["up15_raw_hit_rate"],
            "up15_episode_rate": row["up15_episodes_per_252_sessions"],
            "median_MFE5": row["median_mfe5"],
            "median_MFE10": row["median_mfe10"],
            "p75_MFE10": row["p75_mfe10"],
            "median_MAE10": row["median_mae10"],
            "up8_before_down5_rate": row["up8_before_down5_rate"],
            "ER10": row["median_er10"],
            "ER20": row["median_er20"],
            "swing_persistence": row["swing_persistence"],
            "median_daily_turnover": row["median_daily_turnover_proxy"],
            **pool_corr_by_code[code],
            "2023_24_up8_episode_retention": confirmation["up8_episode_retention"],
            "2023_24_up10_episode_retention": confirmation["up10_episode_retention"],
            "2023_24_mfe_retention": confirmation["median_mfe10_retention"],
            "2025_up8_episode_retention": stress["up8_episode_retention"],
            "2025_up10_episode_retention": stress["up10_episode_retention"],
            "2025_mfe_retention": stress["median_mfe10_retention"],
            "final_classification": final_classification(later, CFG),
            "selection_rationale": "ELIGIBLE_AND_REPEATABLE; SCORE_ORDER; ALL_SELECTED_CORRELATIONS_LE_0.80",
        })

    old_metrics = {}
    for code in old_codes:
        if code not in prepared_by_code:
            raise RuntimeError(f"previous universe stock missing from formal OHLCV: {code}")
        if code in discovery_combined:
            old_metrics[code] = discovery_combined[code]
            continue
        stock = prepared_by_code[code]
        daily = discovery_daily.get(code) or daily_metrics(stock, benchmark.calendar, *PERIODS[0][1:3], PERIODS[0][0], CFG)
        swing, _, _ = swing_metrics(
            stock, daily, PERIODS[0][1], PERIODS[0][2], PERIODS[0][0], PERIODS[0][3], CFG
        )
        old_metrics[code] = {**daily, **{key: value for key, value in swing.items() if key not in {"period", "stock_id", "stock_name"}}}
    old_matrix, old_corr_audit = correlation_matrix(old_codes, prepared_by_code)
    del old_matrix
    new_metrics = {code: discovery_combined[code] for code in selected_codes}
    old_summary = aggregate_universe("PREVIOUS_STABLE_SPECIALIST_UNIVERSE", old_codes, old_metrics, old_corr_audit)
    new_summary = aggregate_universe("HIGH_UPSIDE_SWING_PRIMARY", selected_codes, new_metrics, pool_corr_audit)
    comparison_fields = [key for key in old_summary if key not in {"row_type", "universe", "stock_count"}]
    difference = {"row_type": "ABSOLUTE_DIFFERENCE", "universe": "HIGH_UPSIDE_MINUS_PREVIOUS", "stock_count": len(selected_codes) - len(old_codes)}
    ratio = {"row_type": "RATIO", "universe": "HIGH_UPSIDE_DIVIDED_BY_PREVIOUS", "stock_count": len(selected_codes) / len(old_codes)}
    for field in comparison_fields:
        difference[field] = new_summary[field] - old_summary[field]
        ratio[field] = new_summary[field] / old_summary[field] if old_summary[field] else None
    comparison_rows = [old_summary, new_summary, difference, ratio]

    persistence_cutoff = quantile((row["swing_persistence"] for row in scored), 0.25)
    noise_examples = [
        {
            "stock_id": row["stock_id"], "stock_name": row["stock_name"],
            "median_daily_range_pct": row["median_daily_range_pct"],
            "swing_persistence": row["swing_persistence"],
            "repeatability_status": row["repeatability_status"],
        }
        for row in sorted(scored, key=lambda item: (-item["median_daily_range_pct"], item["stock_id"]))
        if row["swing_persistence"] <= persistence_cutoff and not row["swing_repeatability_pass"]
    ][:5]
    classifications = {
        label: sorted(row["stock_id"] for row in final_rows if row["final_classification"] == label)
        for label in (
            "PERSISTENT_HIGH_UPSIDE_SPECIALIST", "REGIME_HIGH_UPSIDE_SPECIALIST",
            "DISCOVERY_ONLY_HIGH_UPSIDE", "LOST_TRADABILITY",
        )
    }
    validation = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "raw_stock_count": len(stocks),
        "prepared_stock_count": len(prepared),
        "eligibility_pass_count": len(eligible_codes),
        "repeatability_pass_count": len(rankings),
        "primary_pool_count": len(selected_codes),
        "high_correlation_reserve_count": len(high_corr),
        "general_reserve_count": len(reserve),
        "pool_correlation": pool_corr_audit,
        "classifications": classifications,
        "intraday_noise_low_persistence_examples": noise_examples,
        "comparison": comparison_rows,
        "selection_period": "2020-2022",
        "confirmation_period": "2023-2024",
        "stress_period": "2025",
        "future_outcomes_used_for_discovery_universe_characterization": True,
        "later_period_outcomes_used_for_discovery_selection": False,
        "later_period_rerank_count": 0,
        "later_period_reselection_count": 0,
        "model_fit_count": CFG.model_fit_count,
        "stage_a_refit_count": CFG.stage_a_refit_count,
        "actual_orders": CFG.actual_orders,
        "actual_fills": CFG.actual_fills,
        "broker_connections": CFG.broker_connections,
        "checks": {
            "primary_pool_at_most_15": len(selected_codes) <= CFG.maximum_primary_pool_size,
            "all_primary_repeatability_pass": all(row["swing_repeatability_pass"] for row in selected),
            "all_primary_pairwise_correlations_le_0_80": pool_corr_audit["maximum_pairwise_correlation"] <= CFG.maximum_pairwise_correlation,
            "later_selection_mutations_zero": True,
            "cluster_quota_used": False,
            "warrant_research": False,
            "safety_zero": CFG.actual_orders == CFG.actual_fills == CFG.broker_connections == CFG.model_fit_count == CFG.stage_a_refit_count == 0,
        },
    }

    daily_output = [row for row in daily_rows]
    persistence_output = persistence_rows
    primary_output = [
        {
            "primary_selection_order": row["primary_selection_order"], "stock_id": row["stock_id"], "stock_name": row["stock_name"],
            "discovery_rank": row["discovery_rank"], "swing_score": row["swing_score"],
            "repeatability_status": row["repeatability_status"], "selection_status": "PRIMARY_SELECTED",
            "selection_reason": "highest remaining discovery Swing Score with all pairwise correlations <=0.80",
        }
        for row in selected
    ]
    write_csv(ROOT / "eligibility_universe.csv", eligibility_rows)
    write_csv(ROOT / "daily_movement_metrics.csv", daily_output)
    write_csv(ROOT / "swing_opportunity_metrics.csv", swing_rows)
    write_csv(ROOT / "annual_swing_repeatability.csv", annual_rows)
    write_csv(ROOT / "deoverlapped_swing_episodes.csv", episode_rows)
    write_csv(ROOT / "swing_persistence_metrics.csv", persistence_output)
    write_csv(ROOT / "discovery_swing_scores.csv", scored)
    write_csv(ROOT / "discovery_rankings.csv", rankings)
    write_csv(ROOT / "correlation_matrix.csv", matrix_rows)
    write_csv(ROOT / "primary_specialist_pool.csv", primary_output)
    write_csv(ROOT / "high_correlation_reserve.csv", high_corr or [{"status": "NO_HIGH_CORRELATION_RESERVE"}])
    write_csv(ROOT / "high_upside_swing_reserve.csv", reserve or [{"status": "NO_GENERAL_RESERVE"}])
    write_csv(ROOT / "confirmation_2023_2024.csv", confirmation_rows)
    write_csv(ROOT / "stress_2025.csv", stress_rows)
    write_csv(ROOT / "final_high_upside_specialist_universe.csv", final_rows)
    write_csv(ROOT / "comparison_vs_stable_specialist_universe.csv", comparison_rows)
    write_json(ROOT / "validation_summary.json", validation)

    manifest = {
        "study_id": CFG.study_id,
        "status": "COMPLETE",
        "formal_publish_count": 1,
        "config_fingerprint": CFG.fingerprint(),
        "selection_period": "2020-2022",
        "confirmation_period": "2023-2024",
        "stress_period": "2025",
        "future_outcomes_used_for_discovery_universe_characterization": True,
        "later_period_outcomes_used_for_discovery_selection": False,
        "later_period_rerank_count": 0,
        "later_period_reselection_count": 0,
        "input_hashes": input_hashes,
        "previous_universe_hash": old_audit,
        "output_hashes": {name: sha256_file(ROOT / name) for name in OUTPUTS[:-1]},
        "source_hashes": {name: sha256_file(ROOT / name) for name in SOURCE_FILES},
        "documentation_sha256": sha256_file(ROOT / "README.md"),
        "load_audit": load_audit,
        "prepare_audit": prepare_audit,
        "model_fit_count": CFG.model_fit_count,
        "stage_a_refit_count": CFG.stage_a_refit_count,
        "actual_orders": CFG.actual_orders,
        "actual_fills": CFG.actual_fills,
        "broker_connections": CFG.broker_connections,
        "warrant_research": False,
    }
    manifest["manifest_payload_sha256"] = canonical_hash(manifest)
    write_json(ROOT / "run_manifest.json", manifest)
    return validation


def main() -> None:
    parser = argparse.ArgumentParser(description="High-upside swing Specialist Universe discovery")
    parser.add_argument("command", choices=("freeze-spec", "publish"))
    args = parser.parse_args()
    result = freeze_spec() if args.command == "freeze-spec" else publish()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
