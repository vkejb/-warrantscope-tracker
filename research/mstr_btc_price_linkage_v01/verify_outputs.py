#!/usr/bin/env python3
"""Fail-closed verification of the generated research deliverables."""

from pathlib import Path
import json

import pandas as pd


ROOT = Path(__file__).resolve().parent
REQUIRED = [
    "REPORT_zh-TW.md",
    "data/raw/mstr_daily.csv",
    "data/raw/mstr_corporate_actions.csv",
    "data/raw/qqq_daily.csv",
    "data/raw/bitstamp_btcusd_15m.csv",
    "data/processed/daily_aligned.csv",
    "results/rolling_correlation.csv",
    "results/rolling_beta.csv",
    "results/asymmetry.csv",
    "results/lead_lag.csv",
    "results/annual_comparison.csv",
    "results/regime_comparison.csv",
    "results/control_regression.csv",
    "results/structural_change.csv",
    "results/intraday_lead_lag_status.csv",
    "results/source_manifest.json",
    "results/data_quality.json",
    "results/summary_metrics.json",
]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    missing = [name for name in REQUIRED if not (ROOT / name).exists()]
    check(not missing, f"missing deliverables: {missing}")

    daily = pd.read_csv(
        ROOT / "data/processed/daily_aligned.csv",
        parse_dates=["date", "market_open", "market_close", "btc_open_candle_end_utc", "btc_close_candle_end_utc"],
    )
    check(len(daily) >= 1500, f"unexpectedly short aligned sample: {len(daily)}")
    check(daily["date"].min() <= pd.Timestamp("2020-01-03"), "sample does not begin at 2020 start")
    check((daily["btc_open_candle_end_utc"] <= daily["market_open"]).all(), "future BTC candle used at MSTR open")
    check((daily["btc_close_candle_end_utc"] <= daily["market_close"]).all(), "future BTC candle used at MSTR close")
    check(set(daily["period"]) == {"research_2020_2023", "holdout_2024_plus"}, "research/holdout split missing")

    actions = pd.read_csv(ROOT / "data/raw/mstr_corporate_actions.csv")
    split = actions[
        (pd.to_numeric(actions["numerator"], errors="coerce") == 10)
        & (pd.to_numeric(actions["denominator"], errors="coerce") == 1)
    ]
    check((split["date_ny"] == "2024-08-08").any(), "MSTR 10:1 split missing or wrong date")

    quality = json.loads((ROOT / "results/data_quality.json").read_text(encoding="utf-8"))
    check(quality["point_in_time_all_pass"] is True, "quality point-in-time gate failed")
    check(quality["btc_internal_missing_candles"] == 0, "BTC series has internal 15-minute gaps")

    rolling_corr = pd.read_csv(ROOT / "results/rolling_correlation.csv")
    rolling_beta = pd.read_csv(ROOT / "results/rolling_beta.csv")
    for window in (20, 60, 120):
        check(f"pearson_{window}d" in rolling_corr, f"missing {window}d rolling Pearson")
        check(f"spearman_{window}d" in rolling_corr, f"missing {window}d rolling Spearman")
        check(f"beta_{window}d" in rolling_beta, f"missing {window}d rolling beta")

    lead = pd.read_csv(ROOT / "results/lead_lag.csv")
    required_lead = {
        "OVERNIGHT_TO_OPEN_GAP", "OVERNIGHT_BTC_TO_POST_OPEN_MSTR",
        "SAME_SESSION_CLOSE_TO_CLOSE", "PRIOR_BTC_SESSION_TO_NEXT_MSTR_SESSION",
    }
    check(required_lead.issubset(set(lead["analysis"])), "lead/lag specifications incomplete")
    intraday = pd.read_csv(ROOT / "results/intraday_lead_lag_status.csv")
    check(intraday.iloc[0]["status"] == "INSUFFICIENT_INTRADAY_DATA", "intraday limitation is not explicit")

    manifest = json.loads((ROOT / "results/source_manifest.json").read_text(encoding="utf-8"))
    names = {row["name"] for row in manifest}
    check({"MSTR", "MSTR corporate actions", "QQQ", "BTC/USD"}.issubset(names), "source manifest incomplete")
    report = (ROOT / "REPORT_zh-TW.md").read_text(encoding="utf-8")
    for phrase in ("先回答五個問題", "同步相關 vs. 真正預測價值", "INSUFFICIENT_INTRADAY_DATA"):
        check(phrase in report, f"report missing required section: {phrase}")
    print(f"VERIFIED: {len(REQUIRED)} deliverables, {len(daily)} aligned rows, point-in-time and split gates passed")


if __name__ == "__main__":
    main()
