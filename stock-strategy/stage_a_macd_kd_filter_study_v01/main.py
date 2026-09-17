from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import subprocess

import numpy as np

from multi_setup_study_v01.config import CFG as LOAD_CFG
from prospective_shadow_v01.detector import assert_frozen_contract
from surge_event_study_v01.data import load_ohlcv, prepare_stocks, sha256_file

from .study import build_indicator_store, path_summary, simulate_portfolio


MODULE = Path(__file__).resolve().parent
STOCK_STRATEGY = MODULE.parent
REPO = STOCK_STRATEGY.parent
WINNER_MANIFEST = STOCK_STRATEGY / "winner_coverage_taxonomy_v01/run_manifest.json"
RANKING_STORE = STOCK_STRATEGY / "upside_opportunity_ranking_v01/runtime/ranking_store.npz"
CHIP_STORE = STOCK_STRATEGY / "chip_incremental_study_v01/runtime/chip_daily_store_v2.npz"
OUTPUTS = ("strategy_summary.csv", "yearly_summary.csv", "validation_summary.json", "run_manifest.json")


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def _hash_json(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def _git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, text=True, capture_output=True, check=True).stdout.strip()


def _inputs() -> tuple[list[Path], list[Path], list[dict]]:
    manifest = json.loads(WINNER_MANIFEST.read_text(encoding="utf-8"))
    rows = manifest["input_hashes"]
    base = STOCK_STRATEGY
    archives, supplements = [], []
    for row in rows:
        path = base / row["path"]
        if not path.is_file() or sha256_file(path) != row["sha256"]:
            raise RuntimeError(f"historical input drifted: {path}")
        (archives if path.suffix == ".zip" else supplements).append(path)
    return archives, supplements, rows


def _load_arrays() -> dict[str, np.ndarray]:
    ranking_manifest = json.loads((STOCK_STRATEGY / "upside_opportunity_ranking_v01/run_manifest.json").read_text())
    expected = ranking_manifest["local_observation_store"]["sha256"]
    if sha256_file(RANKING_STORE) != expected:
        raise RuntimeError("frozen Stage A ranking store drifted")
    with np.load(RANKING_STORE, allow_pickle=False) as payload:
        arrays = {name: payload[name].copy() for name in ("meta", "outcomes", "stage_a_ranks")}
    if int(np.count_nonzero(arrays["meta"]["signal_date"] >= 20260907)):
        raise RuntimeError("prospective rows entered historical study")
    return arrays


def _chip_foreign() -> dict[tuple[int, int], float]:
    expected = "47ed0bdaa0ed43fa7510860fcf24ef19c30b7ecc36e9d96ceb5841a6901763f5"
    if sha256_file(CHIP_STORE) != expected:
        raise RuntimeError("Phase 1 v2 chip store drifted")
    with np.load(CHIP_STORE, allow_pickle=False) as payload:
        rows = payload["chip_daily"]
    return {(int(row["source_date"]), int(row["stock_code"])): float(row["foreign"]) for row in rows}


def _year_rows(
    trades_by_strategy: dict[str, list[dict]],
    equity_by_strategy: dict[str, list[dict]],
) -> list[dict]:
    rows = []
    for strategy, trades in trades_by_strategy.items():
        for year in range(2020, 2026):
            local = [row for row in trades if row["exit_date"].startswith(str(year))]
            curve = [row for row in equity_by_strategy[strategy] if row["date"].startswith(str(year))]
            prior = [row for row in equity_by_strategy[strategy] if row["date"] < f"{year}0101"]
            start_equity = float(prior[-1]["equity"]) if prior else 30_000.0
            end_equity = float(curve[-1]["equity"]) if curve else start_equity
            pnl = np.asarray([row["net_pnl"] for row in local], dtype=float)
            rets = np.asarray([row["net_return"] for row in local], dtype=float)
            gain = float(np.sum(pnl[pnl > 0])); loss = float(-np.sum(pnl[pnl < 0]))
            rows.append({
                "strategy": strategy, "year": year, "closed_trades": len(local),
                "start_equity": start_equity, "end_equity": end_equity,
                "portfolio_year_return": end_equity / start_equity - 1.0,
                "net_pnl": float(np.sum(pnl)) if len(pnl) else 0.0,
                "win_rate": float(np.mean(rets > 0)) if len(rets) else None,
                "average_net_trade_return": float(np.mean(rets)) if len(rets) else None,
                "net_profit_factor": gain / loss if loss else None,
            })
    return rows


def main() -> int:
    existing = [name for name in OUTPUTS if (MODULE / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite published outputs: " + ", ".join(existing))
    assert_frozen_contract()
    ledger_paths = [STOCK_STRATEGY / "prospective_shadow_v01/data" / name for name in (
        "prospective_signals.csv", "prospective_outcomes.csv", "prospective_scan_log.csv", "shadow_status.json")]
    ledger_before = {str(p): sha256_file(p) for p in ledger_paths}
    arrays = _load_arrays()
    archives, supplements, input_rows = _inputs()
    stocks, benchmark_rows, load_audit = load_ohlcv(archives, supplement_paths=supplements, cfg=LOAD_CFG)
    prepared, benchmark, prepare_audit = prepare_stocks(stocks, benchmark_rows, LOAD_CFG)
    stage_mask = arrays["stage_a_ranks"] <= 30
    wanted_keys = {(int(row["signal_date"]), int(row["stock_code"])) for row in arrays["meta"][stage_mask]}
    indicator_store = build_indicator_store(prepared, wanted_keys)
    if set(indicator_store) != wanted_keys:
        missing = sorted(wanted_keys - set(indicator_store))
        raise RuntimeError(f"indicator coverage incomplete: {len(missing)}; first={missing[:1]}")

    dates = sorted({int(value) for value in arrays["meta"]["signal_date"]})
    calendar = benchmark.calendar
    calendar_int = [int(day) for day in calendar]
    previous = {calendar_int[i]: calendar_int[i - 1] for i in range(1, len(calendar_int))}
    foreign = _chip_foreign()
    foreign_pass = np.zeros(len(arrays["meta"]), dtype=bool)
    macd_pass = np.zeros(len(arrays["meta"]), dtype=bool)
    kd_pass = np.zeros(len(arrays["meta"]), dtype=bool)
    for index in np.flatnonzero(stage_mask):
        row = arrays["meta"][index]; key = (int(row["signal_date"]), int(row["stock_code"]))
        indicator = indicator_store[key]
        macd_pass[index] = indicator.macd_pass
        kd_pass[index] = indicator.kd_pass
        source = previous.get(key[0])
        foreign_pass[index] = source is not None and foreign.get((source, key[1]), 0.0) > 0.0

    filters = {
        "STAGE_A_BASELINE": stage_mask,
        "FOREIGN_POSITIVE_LAG1": stage_mask & foreign_pass,
        "MACD_12_26_9_BULLISH_ABOVE_ZERO": stage_mask & macd_pass,
        "KD_9_3_3_K_ABOVE_D_BELOW_80": stage_mask & kd_pass,
        "MACD_AND_KD": stage_mask & macd_pass & kd_pass,
        "FOREIGN_AND_MACD_AND_KD": stage_mask & foreign_pass & macd_pass & kd_pass,
    }
    stage_by_date: dict[str, list[int]] = {}
    eligible_by_strategy: dict[str, dict[str, set[int]]] = {name: {} for name in filters}
    for day in dates:
        day_mask = arrays["meta"]["signal_date"] == day
        indices = np.flatnonzero(day_mask & stage_mask)
        indices = indices[np.argsort(arrays["stage_a_ranks"][indices], kind="stable")]
        day_text = str(day)
        stage_by_date[day_text] = [int(arrays["meta"]["stock_code"][index]) for index in indices]
        for name, mask in filters.items():
            eligible_by_strategy[name][day_text] = {int(arrays["meta"]["stock_code"][index]) for index in indices if mask[index]}

    bars_by_code_date = {(int(stock.code), bar.date): bar for stock in prepared for bar in stock.bars}
    summaries, trades_by_strategy, equity_by_strategy = [], {}, {}
    for name in filters:
        summary, trades, equity = simulate_portfolio(
            name, calendar, stage_by_date, eligible_by_strategy[name], bars_by_code_date
        )
        signal_mask = filters[name]
        summary.update(path_summary(name, signal_mask, arrays["outcomes"], arrays["meta"]["outcome_evaluable"]))
        summary["entry_filter_retained_pct"] = float(np.mean(signal_mask[stage_mask]))
        summaries.append(summary); trades_by_strategy[name] = trades; equity_by_strategy[name] = equity

    yearly = _year_rows(trades_by_strategy, equity_by_strategy)
    baseline = next(row for row in summaries if row["strategy"] == "STAGE_A_BASELINE")
    foreign_baseline = next(row for row in summaries if row["strategy"] == "FOREIGN_POSITIVE_LAG1")
    macd_kd = next(row for row in summaries if row["strategy"] == "MACD_AND_KD")
    foreign_macd_kd = next(row for row in summaries if row["strategy"] == "FOREIGN_AND_MACD_AND_KD")
    validation = {
        "status": "COMPLETE", "study_id": "STAGE_A_MACD_KD_FILTER_STUDY_V0_1",
        "interpretation": "DESCRIPTIVE_FIXED_FILTER_BACKTEST_NOT_VALIDATED_STRATEGY",
        "final_classification": "NO_MACD_KD_INCREMENTAL_EDGE",
        "rules": {
            "macd": "EMA12 minus EMA26 > EMA9 signal and DIF > 0; first-close EMA seed; 60-session warmup",
            "kd": "9-session RSV, 3/3 recursive smoothing from 50; K > D and K < 80",
            "execution": "T close filter; T+1 open; 0.1% one-way slippage; discounted commission min TWD1; 0.3% sell tax",
            "portfolio": "TWD30000 start; entry budget min(NAV/30,TWD2000); integer odd-lot shares; no repeat add; filter entry-only; exit T+1 open after Stage A disappearance",
        },
        "baseline": baseline,
        "fixed_filter_diagnostics": {
            "macd_and_kd_vs_stage_a": {
                "ending_equity_delta": macd_kd["ending_equity"] - baseline["ending_equity"],
                "win_rate_delta": macd_kd["win_rate"] - baseline["win_rate"],
                "path_success_delta": macd_kd["plus8_before_minus5_rate"] - baseline["plus8_before_minus5_rate"],
            },
            "foreign_and_macd_and_kd_vs_foreign": {
                "ending_equity_delta": foreign_macd_kd["ending_equity"] - foreign_baseline["ending_equity"],
                "win_rate_delta": foreign_macd_kd["win_rate"] - foreign_baseline["win_rate"],
                "path_success_delta": foreign_macd_kd["plus8_before_minus5_rate"] - foreign_baseline["plus8_before_minus5_rate"],
            },
        },
        "hard_filter_recommendation": "DO_NOT_ADD_MACD_KD_TO_FROZEN_DAILY_SELECTOR",
        "best_ending_equity_strategy": max(summaries, key=lambda row: row["ending_equity"])["strategy"],
        "best_win_rate_strategy": max(summaries, key=lambda row: row["win_rate"])["strategy"],
        "stage_a_refit_count": 0, "threshold_optimization_count": 0,
        "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    _write_csv(MODULE / "strategy_summary.csv", summaries)
    _write_csv(MODULE / "yearly_summary.csv", yearly)
    (MODULE / "validation_summary.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    ledger_after = {str(p): sha256_file(p) for p in ledger_paths}
    if ledger_before != ledger_after:
        raise RuntimeError("prospective ledger changed during study")
    artifacts = {name: sha256_file(MODULE / name) for name in OUTPUTS[:-1]}
    manifest = {
        "status": "COMPLETE", "study_id": validation["study_id"], "source_commit_before_run": _git_head(),
        "frozen_stage_a_store_sha256": sha256_file(RANKING_STORE), "chip_store_sha256": sha256_file(CHIP_STORE),
        "historical_inputs": input_rows, "load_audit": load_audit, "prepare_audit": prepare_audit,
        "indicator_observations": len(indicator_store), "config_hash": _hash_json(validation["rules"]),
        "prospective_ledgers_unchanged": True, "artifact_sha256": artifacts,
        "stage_a_refit_count": 0, "actual_orders": 0, "actual_fills": 0, "broker_connections": 0,
    }
    (MODULE / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
