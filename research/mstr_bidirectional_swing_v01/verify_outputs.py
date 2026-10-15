#!/usr/bin/env python3
"""Completion verifier for the fixed MSTR bidirectional study."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent
REQUIRED = [
    "REPORT_zh-TW.md", "params.json", "run_study.py",
    "data/raw/ibit_daily.csv", "data/raw/ibit_corporate_actions.csv", "data/raw/ibit_download_metadata.json",
    "data/processed/research_daily.csv", "results/performance.csv", "results/trades.csv",
    "results/daily_equity.csv", "results/annual_performance.csv", "results/sensitivity.csv",
    "results/m4_incremental_value.csv", "results/risk_normalized_performance.csv",
    "results/m3_incremental_value.csv",
    "results/residual_daily.csv", "results/residual_diagnostic.csv", "results/data_quality.json",
    "results/source_manifest.json", "results/parameter_hash.json", "results/research_status.json",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    missing = [name for name in REQUIRED if not (ROOT / name).exists()]
    check(not missing, f"missing deliverables: {missing}")
    params = json.loads((ROOT / "params.json").read_text(encoding="utf-8"))
    recorded = json.loads((ROOT / "results/parameter_hash.json").read_text(encoding="utf-8"))
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    check(hashlib.sha256(canonical).hexdigest() == recorded["parameter_sha256"], "parameter hash mismatch")

    quality = json.loads((ROOT / "results/data_quality.json").read_text(encoding="utf-8"))
    check(quality["aligned_rows"] >= 1500, "aligned sample too short")
    check(quality["mstr_split_date"] == "2024-08-08" and quality["mstr_split_ratio"] == "10:1", "split gate failed")
    check(quality["ibit_first_date"] == "2024-01-11" and quality["ibit_prelisting_values"] == 0, "IBIT listing gate failed")
    check(quality["btc_close_point_in_time_pass"] and quality["btc_30d_point_in_time_pass"], "BTC point-in-time gate failed")
    check(quality["short_borrow_data_status"] == "INSUFFICIENT_DATA", "SHORT data limitation not explicit")

    daily_source = pd.read_csv(ROOT / "data/processed/research_daily.csv", parse_dates=[
        "date", "market_close", "btc_close_candle_end_utc", "btc_30d_reference_candle_end_utc"
    ])
    check((daily_source["btc_close_candle_end_utc"] <= daily_source["market_close"]).all(), "future BTC close candle")
    available = daily_source["btc_30d_reference_candle_end_utc"].notna()
    target = daily_source.loc[available, "market_close"] - pd.Timedelta(days=params["btc_confirmation_lookback_calendar_days"])
    check((daily_source.loc[available, "btc_30d_reference_candle_end_utc"] <= target).all(), "future BTC 30-day reference")
    expected_prior_high = daily_source["high"].shift(1).rolling(20, min_periods=20).max()
    valid_high = expected_prior_high.notna()
    check(np.isclose(daily_source.loc[valid_high, "prior20_high"], expected_prior_high[valid_high]).all(), "breakout window mismatch")

    performance = pd.read_csv(ROOT / "results/performance.csv")
    required_metrics = {
        "cagr", "net_pnl", "sharpe", "sortino", "profit_factor", "win_rate", "max_drawdown",
        "average_win", "average_loss", "average_holding_trading_days", "long_net_pnl", "short_net_pnl",
        "turnover_annualized", "cash_time_fraction", "transaction_cost", "worst_year_return", "worst_trade",
        "gap_overrun_count", "gap_overrun_max",
        "max_entry_notional_fraction", "max_planned_risk_fraction", "max_mark_to_market_exposure_fraction",
    }
    check(required_metrics.issubset(performance.columns), "required performance metrics missing")
    required_versions = {"M1", "M2", "M3", "M4", "M0_MSTR_BH", "QQQ_BH", "BTC_BH", "IBIT_BH"}
    check(required_versions.issubset(set(performance["version"])), "version or benchmark missing")
    check({"research", "holdout", "full", "common_ibit"}.issubset(set(performance["sample"])), "period split missing")

    trades = pd.read_csv(ROOT / "results/trades.csv", parse_dates=["signal_date", "entry_date", "exit_date"])
    strategy = trades[trades["version"].isin(["M1", "M2", "M3", "M4"])]
    check((strategy["entry_date"] > strategy["signal_date"]).all(), "same-day/future-leaking entry found")
    check(((strategy["planned_risk"] / strategy["entry_equity"]) <= params["risk_per_trade_fraction"] + 1e-9).all(), "risk budget exceeded at sizing")
    check(((strategy["entry_notional"] / strategy["entry_equity"]) <= params["max_notional_fraction"] + 1e-9).all(), "entry notional cap exceeded")
    check(set(strategy.loc[strategy["version"] == "M1", "side"]) <= {"LONG"}, "M1 has SHORT")
    check(set(strategy.loc[strategy["version"] == "M2", "side"]) <= {"SHORT"}, "M2 has LONG")
    m4 = strategy[strategy["version"] == "M4"]
    signal_lookup = daily_source.set_index("date")
    for row in m4.itertuples():
        signal = signal_lookup.loc[row.signal_date]
        check(signal["btc_30d_return"] > 0 if row.side == "LONG" else signal["btc_30d_return"] < 0,
              f"M4 BTC direction mismatch on {row.signal_date}")
    check((strategy.loc[strategy["side"] == "SHORT", "short_qualification"] == "CONDITIONAL_SIMULATION_ONLY").all(), "SHORT qualification mislabeled")
    equity = pd.read_csv(ROOT / "results/daily_equity.csv", parse_dates=["date"])
    for (sample, version), group in equity[equity["version"].isin(["M1", "M2", "M3", "M4"])].groupby(["sample", "version"]):
        group = group.sort_values("date").reset_index(drop=True)
        breach_indices = group.index[group["gross_exposure_fraction"] > params["max_notional_fraction"]].tolist()
        for index in breach_indices:
            if index + 1 < len(group):
                check(group.iloc[index + 1]["position_side"] == "FLAT", f"notional breach not remediated next open: {sample} {version}")

    sensitivity = pd.read_csv(ROOT / "results/sensitivity.csv")
    scenarios = set(sensitivity["scenario"])
    for required in ("double_transaction_cost", "adverse_next_open_fill_1pct", "remove_best_trade"):
        check(required in scenarios, f"missing stress {required}")
    check(any(s.startswith("remove_best_year_") for s in scenarios), "best-year removal missing")
    for rate in (5, 15, 30):
        check(f"borrow_rate_{rate}pct" in scenarios, f"borrow stress {rate}% missing")
    check("short_unavailable_all_entries" in scenarios, "short unavailability stress missing")
    check(f"short_recall_after_{params['short_recall_stress_holding_days']}d" in scenarios, "short recall stress missing")

    incremental = pd.read_csv(ROOT / "results/m4_incremental_value.csv")
    check(set(incremental["sample"]) == {"research", "holdout", "full", "common_ibit"}, "M4 incremental periods incomplete")
    m3_incremental = pd.read_csv(ROOT / "results/m3_incremental_value.csv")
    check(set(m3_incremental["sample"]) == {"research", "holdout", "full", "common_ibit"}, "M3 incremental periods incomplete")
    residual = pd.read_csv(ROOT / "results/residual_diagnostic.csv")
    check(residual["diagnostic_only"].all(), "residual output not marked diagnostic-only")
    status = json.loads((ROOT / "results/research_status.json").read_text(encoding="utf-8"))
    check(status["status"] in {"NO_ROBUST_EDGE", "ELIGIBLE_FOR_PROSPECTIVE_PAPER_SHADOW"}, "invalid final status")
    report = (ROOT / "REPORT_zh-TW.md").read_text(encoding="utf-8")
    for phrase in ("先回答六個問題", "NO_ROBUST_EDGE", "INSUFFICIENT_DATA", "純診斷"):
        check(phrase in report, f"report missing required disclosure: {phrase}")
    print(f"VERIFIED: {len(REQUIRED)} deliverables, {len(strategy)} strategy trades, all point-in-time/risk/split gates passed")


if __name__ == "__main__":
    main()
