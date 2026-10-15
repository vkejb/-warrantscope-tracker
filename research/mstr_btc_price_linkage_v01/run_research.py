#!/usr/bin/env python3
"""Point-in-time MSTR/BTC price-linkage research; no trading logic."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import requests
from urllib3.exceptions import InsecureRequestWarning


ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
UTC = timezone.utc
HOLDOUT_START = pd.Timestamp("2024-01-01")
SPLIT_DATE = pd.Timestamp("2024-08-08")
WINDOWS = (20, 60, 120)
BITSTAMP_STEP_SECONDS = 900
BITSTAMP_BATCH_CANDLES = 1000
INTRADAY_STATUS = "INSUFFICIENT_INTRADAY_DATA"


@dataclass
class SourceFile:
    name: str
    path: Path
    url: str
    rows: int
    first_timestamp: str | None
    last_timestamp: str | None


def ensure_dirs() -> None:
    for path in (RAW, PROCESSED, RESULTS):
        path.mkdir(parents=True, exist_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def session(insecure_tls: bool) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "WarrantScope-MSTR-BTC-Research/1.0"})
    s.verify = not insecure_tls
    if insecure_tls:
        warnings.simplefilter("ignore", InsecureRequestWarning)
    return s


def request_json(s: requests.Session, url: str, params: dict | None = None, attempts: int = 6):
    delay = 0.8
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = s.get(url, params=params, timeout=45)
            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # bounded retry for public read-only APIs
            last_error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(delay)
            delay = min(delay * 2, 12)
    raise RuntimeError(f"download failed for {url}: {last_error}")


def yahoo_daily(s: requests.Session, symbol: str, start: str, end_exclusive: str, refresh: bool) -> tuple[pd.DataFrame, pd.DataFrame, SourceFile, SourceFile]:
    out = RAW / f"{symbol.lower()}_daily.csv"
    actions_out = RAW / f"{symbol.lower()}_corporate_actions.csv"
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    if refresh or not out.exists() or not actions_out.exists():
        p1 = int(pd.Timestamp(start, tz="UTC").timestamp())
        p2 = int(pd.Timestamp(end_exclusive, tz="UTC").timestamp())
        payload = request_json(s, url, {
            "period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"
        })
        error = payload.get("chart", {}).get("error")
        result = payload.get("chart", {}).get("result")
        if error or not result:
            raise RuntimeError(f"Yahoo {symbol} payload error: {error}")
        chart = result[0]
        quote = chart["indicators"]["quote"][0]
        adjusted = chart["indicators"].get("adjclose", [{}])[0].get("adjclose", [])
        rows = []
        for i, stamp in enumerate(chart["timestamp"]):
            row = {
                "timestamp_utc": pd.to_datetime(stamp, unit="s", utc=True),
                "date": pd.to_datetime(stamp, unit="s", utc=True).tz_convert("America/New_York").date().isoformat(),
            }
            for col in ("open", "high", "low", "close", "volume"):
                values = quote.get(col, [])
                row[col] = values[i] if i < len(values) else None
            row["adj_close"] = adjusted[i] if i < len(adjusted) else None
            rows.append(row)
        frame = pd.DataFrame(rows).sort_values("date").drop_duplicates("date")
        frame.to_csv(out, index=False)

        actions = []
        for kind, entries in chart.get("events", {}).items():
            for event in entries.values():
                stamp = int(event["date"])
                actions.append({
                    "timestamp_utc": pd.to_datetime(stamp, unit="s", utc=True),
                    "date_ny": pd.to_datetime(stamp, unit="s", utc=True).tz_convert("America/New_York").date().isoformat(),
                    "event_type": kind,
                    "split_ratio": event.get("splitRatio"),
                    "numerator": event.get("numerator"),
                    "denominator": event.get("denominator"),
                    "amount": event.get("amount"),
                })
        pd.DataFrame(actions, columns=[
            "timestamp_utc", "date_ny", "event_type", "split_ratio", "numerator", "denominator", "amount"
        ]).to_csv(actions_out, index=False)
    frame = pd.read_csv(out, parse_dates=["timestamp_utc"])
    actions = pd.read_csv(actions_out)
    src = SourceFile(symbol, out, url, len(frame), str(frame["timestamp_utc"].min()), str(frame["timestamp_utc"].max()))
    action_src = SourceFile(
        f"{symbol} corporate actions", actions_out, url, len(actions),
        str(actions["timestamp_utc"].min()) if len(actions) else None,
        str(actions["timestamp_utc"].max()) if len(actions) else None,
    )
    return frame, actions, src, action_src


def bitstamp_15m(s: requests.Session, start: str, end_exclusive: str, refresh: bool) -> tuple[pd.DataFrame, SourceFile]:
    out = RAW / "bitstamp_btcusd_15m.csv"
    url = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
    start_ts = int(pd.Timestamp(start, tz="UTC").timestamp())
    end_ts = int(pd.Timestamp(end_exclusive, tz="UTC").timestamp())
    if refresh or not out.exists():
        records: dict[int, dict] = {}
        cursor = start_ts
        span = BITSTAMP_STEP_SECONDS * (BITSTAMP_BATCH_CANDLES - 1)
        batch = 0
        while cursor < end_ts:
            batch_end = min(cursor + span, end_ts - BITSTAMP_STEP_SECONDS)
            payload = request_json(s, url, {
                "step": BITSTAMP_STEP_SECONDS,
                "limit": BITSTAMP_BATCH_CANDLES,
                "start": cursor,
                "end": batch_end,
                "exclude_current_candle": "true",
            })
            candles = payload.get("data", {}).get("ohlc", [])
            if not candles:
                raise RuntimeError(f"Bitstamp empty batch at {pd.to_datetime(cursor, unit='s', utc=True)}")
            for candle in candles:
                stamp = int(candle["timestamp"])
                if start_ts <= stamp < end_ts:
                    records[stamp] = candle
            cursor = batch_end + BITSTAMP_STEP_SECONDS
            batch += 1
            if batch % 25 == 0:
                print(f"Bitstamp batches={batch}, through={pd.to_datetime(cursor, unit='s', utc=True)}", flush=True)
            time.sleep(0.10)
        rows = []
        for stamp, candle in sorted(records.items()):
            rows.append({
                "candle_start_utc": pd.to_datetime(stamp, unit="s", utc=True),
                "candle_end_utc": pd.to_datetime(stamp + BITSTAMP_STEP_SECONDS, unit="s", utc=True),
                "open": float(candle["open"]), "high": float(candle["high"]),
                "low": float(candle["low"]), "close": float(candle["close"]),
                "volume_btc": float(candle["volume"]),
            })
        pd.DataFrame(rows).to_csv(out, index=False)
    frame = pd.read_csv(out, parse_dates=["candle_start_utc", "candle_end_utc"])
    src = SourceFile("BTC/USD", out, url, len(frame), str(frame["candle_start_utc"].min()), str(frame["candle_end_utc"].max()))
    return frame, src


def last_known(candles: pd.DataFrame, timestamps: pd.Series) -> pd.DataFrame:
    left = pd.DataFrame({"target_utc": pd.to_datetime(timestamps, utc=True)}).sort_values("target_utc")
    right = candles[["candle_end_utc", "close"]].dropna().sort_values("candle_end_utc")
    result = pd.merge_asof(left, right, left_on="target_utc", right_on="candle_end_utc", direction="backward")
    result["staleness_seconds"] = (result["target_utc"] - result["candle_end_utc"]).dt.total_seconds()
    return result.sort_index()


def validate_split(mstr: pd.DataFrame, actions: pd.DataFrame) -> dict:
    splits = actions[actions["event_type"].astype(str).str.contains("split", case=False, na=False)].copy()
    ten_for_one = splits[
        np.isclose(pd.to_numeric(splits["numerator"], errors="coerce"), 10.0)
        & np.isclose(pd.to_numeric(splits["denominator"], errors="coerce"), 1.0)
    ]
    if ten_for_one.empty:
        raise AssertionError("MSTR 2024 10:1 split event missing")
    event_dates = pd.to_datetime(ten_for_one["date_ny"])
    if not (event_dates == SPLIT_DATE).any():
        raise AssertionError(f"10:1 split date mismatch: {event_dates.tolist()}")
    indexed = mstr.assign(date=pd.to_datetime(mstr["date"])).set_index("date").sort_index()
    around = indexed.loc["2024-08-07":"2024-08-09", "close"].pct_change().abs().max()
    if pd.isna(around) or around > 0.50:
        raise AssertionError(f"split-adjusted OHLC continuity failed: max move={around}")
    return {"split_date": SPLIT_DATE.date().isoformat(), "ratio": "10:1", "continuity_max_abs_return": float(around)}


def build_daily(mstr: pd.DataFrame, qqq: pd.DataFrame, btc: pd.DataFrame) -> pd.DataFrame:
    m = mstr.copy()
    m["date"] = pd.to_datetime(m["date"])
    q = qqq.copy()
    q["date"] = pd.to_datetime(q["date"])
    q = q[["date", "adj_close"]].rename(columns={"adj_close": "qqq_adj_close"})
    daily = m.merge(q, on="date", how="left", validate="one_to_one")

    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.schedule(start_date=daily["date"].min(), end_date=daily["date"].max())
    schedule = schedule.reset_index().rename(columns={"index": "date"})
    schedule["date"] = pd.to_datetime(schedule["date"]).dt.tz_localize(None)
    daily = daily.merge(schedule[["date", "market_open", "market_close"]], on="date", how="left", validate="one_to_one")
    if daily[["market_open", "market_close"]].isna().any().any():
        missing = daily.loc[daily["market_open"].isna(), "date"].dt.date.astype(str).tolist()
        raise AssertionError(f"NYSE schedule missing for MSTR dates: {missing[:10]}")

    btc = btc.sort_values("candle_end_utc").copy()
    for prefix, target in (("open", daily["market_open"]), ("close", daily["market_close"])):
        known = last_known(btc, target).reset_index(drop=True)
        daily[f"btc_{prefix}"] = known["close"]
        daily[f"btc_{prefix}_candle_end_utc"] = known["candle_end_utc"]
        daily[f"btc_{prefix}_staleness_seconds"] = known["staleness_seconds"]

    daily = daily.sort_values("date").reset_index(drop=True)
    daily["prev_market_close"] = daily["market_close"].shift(1)
    daily["btc_prev_close"] = daily["btc_close"].shift(1)
    daily["btc_prev_close_candle_end_utc"] = daily["btc_close_candle_end_utc"].shift(1)
    daily["mstr_prev_close"] = daily["close"].shift(1)
    daily["mstr_log_return"] = np.log(daily["close"] / daily["mstr_prev_close"])
    daily["mstr_open_to_close_log_return"] = np.log(daily["close"] / daily["open"])
    daily["qqq_log_return"] = np.log(daily["qqq_adj_close"] / daily["qqq_adj_close"].shift(1))
    daily["btc_session_log_return"] = np.log(daily["btc_close"] / daily["btc_prev_close"])
    daily["btc_overnight_log_return"] = np.log(daily["btc_open"] / daily["btc_prev_close"])
    daily["mstr_open_gap_log_return"] = np.log(daily["open"] / daily["mstr_prev_close"])
    daily["btc_prior_session_log_return"] = daily["btc_session_log_return"].shift(1)
    daily["same_direction"] = np.sign(daily["mstr_log_return"]) == np.sign(daily["btc_session_log_return"])
    daily["period"] = np.where(daily["date"] < HOLDOUT_START, "research_2020_2023", "holdout_2024_plus")
    daily["year"] = daily["date"].dt.year
    daily["btc_rolling_vol_60"] = daily["btc_session_log_return"].rolling(60, min_periods=60).std() * np.sqrt(252)
    cutoff = daily.loc[daily["period"] == "research_2020_2023", "btc_rolling_vol_60"].median()
    daily["vol_regime"] = np.where(daily["btc_rolling_vol_60"].isna(), "WARMUP", np.where(daily["btc_rolling_vol_60"] > cutoff, "HIGH", "LOW"))
    daily["btc_direction"] = np.where(daily["btc_session_log_return"] >= 0, "UP", "DOWN")
    daily["open_point_in_time_pass"] = daily["btc_open_candle_end_utc"] <= daily["market_open"]
    daily["close_point_in_time_pass"] = daily["btc_close_candle_end_utc"] <= daily["market_close"]
    ordered = [
        "date", "period", "year", "market_open", "market_close", "open", "high", "low", "close", "adj_close", "volume",
        "mstr_prev_close", "mstr_log_return", "mstr_open_gap_log_return", "mstr_open_to_close_log_return", "qqq_adj_close", "qqq_log_return",
        "btc_prev_close", "btc_open", "btc_close", "btc_overnight_log_return", "btc_session_log_return",
        "btc_prior_session_log_return", "btc_open_candle_end_utc", "btc_close_candle_end_utc",
        "btc_open_staleness_seconds", "btc_close_staleness_seconds", "open_point_in_time_pass", "close_point_in_time_pass",
        "same_direction", "btc_direction", "btc_rolling_vol_60", "vol_regime",
    ]
    return daily[ordered]


def regression(y: pd.Series, x: pd.DataFrame | pd.Series) -> dict:
    xdf = x.to_frame() if isinstance(x, pd.Series) else x.copy()
    data = pd.concat([y.rename("y"), xdf], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(data) < max(10, len(xdf.columns) + 3):
        return {"n": len(data), "r2": np.nan}
    # OLS plus HC3 heteroskedasticity-robust covariance, implemented with numpy
    # so the research is rerunnable without a compiled stats package.
    yv = data["y"].to_numpy(dtype=float)
    xv = np.column_stack([np.ones(len(data)), data[xdf.columns].to_numpy(dtype=float)])
    xtx_inv = np.linalg.pinv(xv.T @ xv)
    params = xtx_inv @ xv.T @ yv
    residuals = yv - xv @ params
    centered = yv - yv.mean()
    ss_total = float(centered @ centered)
    r2 = 1.0 - float(residuals @ residuals) / ss_total if ss_total > 0 else np.nan
    leverage = np.sum((xv @ xtx_inv) * xv, axis=1)
    scaled = residuals / np.clip(1.0 - leverage, 1e-12, None)
    meat = xv.T @ ((scaled ** 2)[:, None] * xv)
    covariance = xtx_inv @ meat @ xtx_inv
    se = np.sqrt(np.clip(np.diag(covariance), 0.0, None))
    tvalues = np.divide(params, se, out=np.full_like(params, np.nan), where=se > 0)
    # With N > 1000 in the main study, the two-sided normal approximation is
    # effectively identical to the large-df t reference. This is documented.
    pvalues = np.array([math.erfc(abs(float(t)) / math.sqrt(2)) if np.isfinite(t) else np.nan for t in tvalues])
    result = {"n": len(data), "r2": float(r2), "intercept": float(params[0]), "pvalue_reference": "two-sided normal, HC3 SE"}
    for index, col in enumerate(xdf.columns, start=1):
        result[f"beta_{col}"] = float(params[index])
        result[f"pvalue_{col}"] = float(pvalues[index])
        result[f"tstat_{col}"] = float(tvalues[index])
    return result


def spearman_corr(left: pd.Series, right: pd.Series) -> float:
    data = pd.concat([left.rename("left"), right.rename("right")], axis=1).dropna()
    if len(data) < 2:
        return np.nan
    return float(data["left"].rank(method="average").corr(data["right"].rank(method="average")))


def corr_metrics(frame: pd.DataFrame) -> dict:
    d = frame[["mstr_log_return", "btc_session_log_return"]].dropna()
    return {
        "n": len(d),
        "pearson": float(d.corr(method="pearson").iloc[0, 1]),
        "spearman": spearman_corr(d.iloc[:, 0], d.iloc[:, 1]),
        "direction_agreement": float((np.sign(d.iloc[:, 0]) == np.sign(d.iloc[:, 1])).mean()),
        "mstr_vol_annualized": float(d.iloc[:, 0].std(ddof=1) * np.sqrt(252)),
        "btc_vol_annualized": float(d.iloc[:, 1].std(ddof=1) * np.sqrt(252)),
        "volatility_ratio_mstr_to_btc": float(d.iloc[:, 0].std(ddof=1) / d.iloc[:, 1].std(ddof=1)),
        **regression(d["mstr_log_return"], d[["btc_session_log_return"]]),
    }


def rolling_outputs(daily: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    corr = daily[["date", "period"]].copy()
    betas = daily[["date", "period"]].copy()
    for window in WINDOWS:
        corr[f"pearson_{window}d"] = daily["mstr_log_return"].rolling(window, min_periods=window).corr(daily["btc_session_log_return"])
        corr[f"spearman_{window}d"] = daily["mstr_log_return"].rolling(window, min_periods=window).apply(
            lambda values: values.rank().corr(daily.loc[values.index, "btc_session_log_return"].rank()), raw=False
        )
        cov = daily["mstr_log_return"].rolling(window, min_periods=window).cov(daily["btc_session_log_return"])
        var = daily["btc_session_log_return"].rolling(window, min_periods=window).var()
        betas[f"beta_{window}d"] = cov / var
        betas[f"r2_{window}d"] = corr[f"pearson_{window}d"] ** 2
    return corr, betas


def grouped_metrics(daily: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    rows = []
    grouper = group_cols[0] if len(group_cols) == 1 else group_cols
    for keys, frame in daily.groupby(grouper, dropna=False):
        keys = (keys,) if not isinstance(keys, tuple) else keys
        row = dict(zip(group_cols, keys))
        row.update(corr_metrics(frame))
        rows.append(row)
    return pd.DataFrame(rows)


def asymmetry(daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for period in ("all", "research_2020_2023", "holdout_2024_plus"):
        base = daily if period == "all" else daily[daily["period"] == period]
        for direction, mask in (("BTC_UP", base["btc_session_log_return"] >= 0), ("BTC_DOWN", base["btc_session_log_return"] < 0)):
            part = base[mask]
            row = {"period": period, "test": "direction_beta", "condition": direction}
            row.update(corr_metrics(part))
            row["mean_mstr_return"] = part["mstr_log_return"].mean()
            rows.append(row)
        for threshold in (0.03, 0.05):
            for direction, mask in (
                ("UP", base["btc_session_log_return"] > threshold),
                ("DOWN", base["btc_session_log_return"] < -threshold),
            ):
                part = base[mask].dropna(subset=["mstr_log_return", "btc_session_log_return"])
                rows.append({
                    "period": period, "test": "extreme_move", "condition": f"BTC_{direction}_{int(threshold*100)}PCT",
                    "n": len(part), "direction_agreement": (np.sign(part["mstr_log_return"]) == np.sign(part["btc_session_log_return"])).mean(),
                    "mean_mstr_return": part["mstr_log_return"].mean(), "median_mstr_return": part["mstr_log_return"].median(),
                    "pearson": part["mstr_log_return"].corr(part["btc_session_log_return"]),
                })
    return pd.DataFrame(rows)


def lead_lag(daily: pd.DataFrame) -> pd.DataFrame:
    specifications = [
        ("OVERNIGHT_TO_OPEN_GAP", "btc_overnight_log_return", "mstr_open_gap_log_return", True,
         "BTC from prior US close through current open; all BTC candles end no later than MSTR open"),
        ("OVERNIGHT_BTC_TO_POST_OPEN_MSTR", "btc_overnight_log_return", "mstr_open_to_close_log_return", True,
         "BTC overnight return is known at MSTR open; target is MSTR open-to-close after the gap is observable"),
        ("SAME_SESSION_CLOSE_TO_CLOSE", "btc_session_log_return", "mstr_log_return", False,
         "Synchronous explanatory relation, not predictive"),
        ("PRIOR_BTC_SESSION_TO_NEXT_MSTR_SESSION", "btc_prior_session_log_return", "mstr_log_return", True,
         "BTC return was fully known at the prior MSTR close"),
    ]
    rows = []
    for period in ("all", "research_2020_2023", "holdout_2024_plus"):
        base = daily if period == "all" else daily[daily["period"] == period]
        for name, xcol, ycol, predictive, note in specifications:
            d = base[[xcol, ycol]].dropna()
            reg = regression(d[ycol], d[[xcol]])
            rows.append({
                "period": period, "analysis": name, "predictive_information_test": predictive, "n": len(d),
                "pearson": d[xcol].corr(d[ycol], method="pearson"), "spearman": spearman_corr(d[xcol], d[ycol]),
                "direction_agreement": (np.sign(d[xcol]) == np.sign(d[ycol])).mean(), "mean_target_return": d[ycol].mean(),
                **reg, "interpretation_boundary": note,
            })
    return pd.DataFrame(rows)


def control_regressions(daily: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for period in ("all", "research_2020_2023", "holdout_2024_plus"):
        d = daily if period == "all" else daily[daily["period"] == period]
        qqq_only = regression(d["mstr_log_return"], d[["qqq_log_return"]])
        full = regression(d["mstr_log_return"], d[["qqq_log_return", "btc_session_log_return"]])
        rows.append({
            "period": period, "n": full.get("n"), "r2_qqq_only": qqq_only.get("r2"), "r2_qqq_plus_btc": full.get("r2"),
            "incremental_r2_btc": full.get("r2", np.nan) - qqq_only.get("r2", np.nan),
            "beta_qqq_controlled": full.get("beta_qqq_log_return"), "pvalue_qqq": full.get("pvalue_qqq_log_return"),
            "beta_btc_controlled": full.get("beta_btc_session_log_return"), "pvalue_btc": full.get("pvalue_btc_session_log_return"),
        })
    return pd.DataFrame(rows)


def structural_change(daily: pd.DataFrame) -> pd.DataFrame:
    d = daily[["date", "mstr_log_return", "btc_session_log_return"]].dropna().copy()
    d["post_2024"] = (d["date"] >= HOLDOUT_START).astype(float)
    d["btc_x_post_2024"] = d["btc_session_log_return"] * d["post_2024"]
    fixed_holdout = regression(d["mstr_log_return"], d[["btc_session_log_return", "post_2024", "btc_x_post_2024"]])
    d["post_split"] = (d["date"] >= SPLIT_DATE).astype(float)
    d["btc_x_post_split"] = d["btc_session_log_return"] * d["post_split"]
    fixed_split = regression(d["mstr_log_return"], d[["btc_session_log_return", "post_split", "btc_x_post_split"]])
    return pd.DataFrame([
        {"fixed_break": "2024-01-01_HOLDOUT_START", **fixed_holdout,
         "beta_before": fixed_holdout.get("beta_btc_session_log_return"),
         "beta_change_after": fixed_holdout.get("beta_btc_x_post_2024"),
         "interaction_pvalue": fixed_holdout.get("pvalue_btc_x_post_2024"),
         "attribution_limit": "Price data detects coefficient change but cannot attribute it to capital structure or valuation."},
        {"fixed_break": "2024-08-08_10_FOR_1_SPLIT", **fixed_split,
         "beta_before": fixed_split.get("beta_btc_session_log_return"),
         "beta_change_after": fixed_split.get("beta_btc_x_post_split"),
         "interaction_pvalue": fixed_split.get("pvalue_btc_x_post_split"),
         "attribution_limit": "Split is mechanical; any coefficient change is descriptive, not proof of a split effect."},
    ])


def format_pct(value: float | None) -> str:
    return "NA" if value is None or not np.isfinite(value) else f"{value * 100:.2f}%"


def format_num(value: float | None, digits: int = 3) -> str:
    return "NA" if value is None or not np.isfinite(value) else f"{value:.{digits}f}"


def create_report(daily: pd.DataFrame, summary: dict, asym: pd.DataFrame, lead: pd.DataFrame,
                  annual: pd.DataFrame, regimes: pd.DataFrame, controls: pd.DataFrame,
                  structure: pd.DataFrame, quality: dict) -> None:
    allm = summary["all"]
    up = asym[(asym.period == "all") & (asym.condition == "BTC_UP")].iloc[0]
    down = asym[(asym.period == "all") & (asym.condition == "BTC_DOWN")].iloc[0]
    overnight = lead[(lead.period == "all") & (lead.analysis == "OVERNIGHT_TO_OPEN_GAP")].iloc[0]
    post_open = lead[(lead.period == "all") & (lead.analysis == "OVERNIGHT_BTC_TO_POST_OPEN_MSTR")].iloc[0]
    prior = lead[(lead.period == "all") & (lead.analysis == "PRIOR_BTC_SESSION_TO_NEXT_MSTR_SESSION")].iloc[0]
    synchronous = lead[(lead.period == "all") & (lead.analysis == "SAME_SESSION_CLOSE_TO_CLOSE")].iloc[0]
    control_all = controls[controls.period == "all"].iloc[0]
    holdout = summary["holdout_2024_plus"]
    year_lines = ["| 年度 | N | Pearson | Beta | R² | 同向率 |", "|---:|---:|---:|---:|---:|---:|"]
    for row in annual.itertuples(index=False):
        year_lines.append(
            f"| {int(row.year)} | {int(row.n)} | {row.pearson:.3f} | "
            f"{row.beta_btc_session_log_return:.3f} | {row.r2:.3f} | {row.direction_agreement:.3f} |"
        )
    year_table = "\n".join(year_lines)
    predictive_weak = abs(prior["pearson"]) < 0.10 and prior["r2"] < 0.02
    post_open_weak = abs(post_open["pearson"]) < 0.10 and post_open["r2"] < 0.02
    next_stage = "尚不足以僅憑本研究進入策略設計" if (predictive_weak and post_open_weak) else "可作為下一階段候選訊號，但仍須前瞻驗證"
    report = f"""# MSTR 與 BTC 價格連動性研究（繁中報告）

資料截止：{daily['date'].max().date().isoformat()}

研究期：2020-01-01～2023-12-31；保留期：2024-01-01～資料截止

範圍：價格關係研究，不含交易策略、門檻調參或下單。

## 先回答五個問題

1. **MSTR 與 BTC 相關性有多高？** 同一美股收盤區間的日對數報酬 Pearson 為 **{allm['pearson']:.3f}**、Spearman 為 **{allm['spearman']:.3f}**（N={allm['n']}）。價格水準的相關性是 {summary['price_level_pearson']:.3f}，但它受共同趨勢影響，**不能視為預測證據**。
2. **BTC 漲跌 1%，MSTR 平均反應多大？** 全期同步回歸 Beta 為 **{allm['beta_btc_session_log_return']:.2f}**，即 BTC 同一區間變動 1% 時，MSTR 平均同步變動約 {allm['beta_btc_session_log_return']:.2f}%；R²={allm['r2']:.3f}。這是同步敏感度，不是事前預測。
3. **BTC 下跌時 MSTR 是否更敏感？** BTC 上漲樣本 Beta={up['beta_btc_session_log_return']:.2f}，下跌樣本 Beta={down['beta_btc_session_log_return']:.2f}；下跌 Beta {'較高' if down['beta_btc_session_log_return'] > up['beta_btc_session_log_return'] else '未較高'}。詳細樣本數與門檻事件見 `results/asymmetry.csv`。
4. **BTC 變動是否對 MSTR 有領先資訊？** 休市期間 BTC 到開盤缺口的回歸 R²={overnight['r2']:.3f}、方向一致率={format_pct(overnight['direction_agreement'])}，顯示資訊會反映在開盤價；但開盤後才可交易時，overnight BTC 對 MSTR open-to-close 的 Pearson={post_open['pearson']:.3f}、R²={post_open['r2']:.3f}。前一完整 BTC 美股區間對下一 MSTR 日報酬的 Pearson={prior['pearson']:.3f}、R²={prior['r2']:.3f}。相較之下，同期 close-to-close 關係 R²={synchronous['r2']:.3f}。因此強證據主要是**同步連動與開盤價格發現**；開盤後/下一日可用的事前資訊{'偏弱' if (predictive_weak and post_open_weak) else '存在但仍需前瞻驗證'}。歷史 MSTR 分鐘資料未取得，5/15/30/60 分鐘測試標為 `{INTRADAY_STATUS}`，沒有用日線補造。
5. **是否穩定到值得進入下一階段交易策略研究？** 保留期 Pearson={holdout['pearson']:.3f}、Beta={holdout['beta_btc_session_log_return']:.2f}、R²={holdout['r2']:.3f}；結論為：**{next_stage}**。高同步相關本身不等於可交易的領先訊號。

## 資料與 point-in-time 對齊

- MSTR/QQQ 來自 Yahoo Finance Chart API；BTC/USD 來自 Bitstamp 15 分鐘 OHLCV。
- 美股日曆採 NYSE schedule，UTC 時戳依 America/New_York DST 對應，並保留提早收盤。
- `btc_open` 只取 candle end `<= market_open` 的完整 K 線；所有 {quality['aligned_rows']} 筆可評估列的開盤/收盤 point-in-time 檢查均通過：{quality['point_in_time_all_pass']}。
- MSTR 10:1 split 事件日期為 {quality['mstr_split']['split_date']}；原始 OHLC 為 split-adjusted 尺度，事件附近最大絕對 close-to-close 報酬為 {format_pct(quality['mstr_split']['continuity_max_abs_return'])}，未出現機械式 -90% 跳變。
- BTC 15 分鐘完整性：來源最新完整 candle 前應有 {quality['btc_contiguous_expected_candles']} 根、實得 {quality['btc_rows']} 根、內部缺口 {quality['btc_internal_missing_candles']} 根；請求終點晚於來源最新完整 candle 的尾端差額為 {quality['btc_unavailable_tail_candles']} 根，不列為資料缺口。開收盤最大 staleness 分別為 {quality['max_open_staleness_seconds']:.0f}/{quality['max_close_staleness_seconds']:.0f} 秒。
- 本次 TLS 驗證：{quality['tls_verification']}。原始檔 URL、時段、列數及 SHA-256 見 `results/source_manifest.json`。

## 同步連動性

- 全期日報酬 Pearson={allm['pearson']:.3f}、Spearman={allm['spearman']:.3f}、方向一致率={format_pct(allm['direction_agreement'])}。
- MSTR 年化波動率={format_pct(allm['mstr_vol_annualized'])}，BTC={format_pct(allm['btc_vol_annualized'])}，波動率比={allm['volatility_ratio_mstr_to_btc']:.2f}。
- 20/60/120 日 rolling Pearson/Spearman 與 Beta/R² 分別保存在 `rolling_correlation.csv`、`rolling_beta.csv`。
- BTC 單日超過 ±3%、±5% 的樣本數、MSTR 同向率與平均報酬保存在 `asymmetry.csv`，未因結果更改門檻。

## QQQ 控制與額外解釋力

全期 QQQ-only R²={control_all['r2_qqq_only']:.3f}；加入 BTC 後 R²={control_all['r2_qqq_plus_btc']:.3f}，BTC incremental R²={control_all['incremental_r2_btc']:.3f}；控制 QQQ 後 BTC Beta={control_all['beta_btc_controlled']:.2f}、p={control_all['pvalue_btc']:.3g}。這支持的是同期額外解釋力，不自動轉化為預測力。

## 年度與環境穩定性

{year_table}

`regime_comparison.csv` 另列研究/保留期 × BTC 上下跌 × 高低波動環境。高低波動切點只由研究期 60 日 BTC 年化波動中位數決定，再原封不動套用保留期。

固定斷點交互作用檢查列於 `structural_change.csv`：2024-01-01 是預先指定的保留期起點，2024-08-08 是 10:1 split。價格資料可檢出 Beta 是否改變，但**不能把變化歸因於資本結構或估值原因**；要做因果歸因需另接公司融資、可轉債、股本與 NAV premium 資料，超出本輪價格研究範圍。

## 同步相關 vs. 真正預測價值

同步 close-to-close 回歸使用同一時間區間的 BTC 和 MSTR 報酬，回答「一起動多少」，不回答「BTC 是否先知道」。真正的事前測試只有：

1. 上一美股收盤至本次開盤、且 BTC K 線在開盤前已完整結束的 overnight return，分別對開盤缺口及開盤後 open-to-close 報酬；
2. 前一美股 close-to-close BTC 報酬對下一 MSTR 日報酬。

overnight BTC 對開盤缺口即使很強，也可能在可成交前已由 gap 完成價格發現，不能直接當成開盤後 alpha。

這兩者的結果不應被同步高相關掩蓋。盤中 5/15/30/60 分鐘因缺少同期間、可驗證且帶時戳的 MSTR 分鐘資料而不執行，狀態為 `{INTRADAY_STATUS}`。

## 限制

- Bitstamp 是單一 BTC/USD 現貨場所，不代表全市場 consolidated price。
- 日線 MSTR open/close 來自資料供應商，BTC 是 15 分鐘最後完整 candle；最大 15 分鐘解析度誤差由 staleness 欄位明示。
- 公司行動事件與價格來自同一供應商；已做 split 事件及連續性檢查，但不是交易所官方公司行動主檔。
- 本研究是歷史觀察；沒有前瞻 shadow 驗證、交易成本、可成交性或策略績效。

## 重跑

見 `README.md`。核心指令：

```bash
python3 run_research.py --start 2020-01-01
python3 -m unittest discover -s tests -v
```
"""
    (ROOT / "REPORT_zh-TW.md").write_text(report, encoding="utf-8")


def manifest(files: Iterable[SourceFile], insecure_tls: bool, generated_at: str) -> list[dict]:
    return [{
        "name": item.name, "source_url": item.url, "local_file": str(item.path.relative_to(ROOT)),
        "rows": item.rows, "first_timestamp": item.first_timestamp, "last_timestamp": item.last_timestamp,
        "sha256": sha256(item.path), "downloaded_or_verified_at_utc": generated_at,
        "tls_verification": not insecure_tls,
    } for item in files]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default=None, help="exclusive UTC date; defaults to tomorrow")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--insecure-tls", action="store_true")
    args = parser.parse_args()
    ensure_dirs()
    today = pd.Timestamp.now(tz="UTC").normalize()
    end_exclusive = args.end or (today + pd.Timedelta(days=1)).date().isoformat()
    generated_at = datetime.now(UTC).isoformat()
    s = session(args.insecure_tls)

    mstr, mstr_actions, mstr_src, mstr_actions_src = yahoo_daily(s, "MSTR", args.start, end_exclusive, args.refresh)
    qqq, _, qqq_src, qqq_actions_src = yahoo_daily(s, "QQQ", args.start, end_exclusive, args.refresh)
    btc, btc_src = bitstamp_15m(s, args.start, end_exclusive, args.refresh)
    split_check = validate_split(mstr, mstr_actions)
    daily = build_daily(mstr, qqq, btc)
    daily.to_csv(PROCESSED / "daily_aligned.csv", index=False)

    rolling_corr, rolling_beta = rolling_outputs(daily)
    rolling_corr.to_csv(RESULTS / "rolling_correlation.csv", index=False)
    rolling_beta.to_csv(RESULTS / "rolling_beta.csv", index=False)
    asym = asymmetry(daily)
    asym.to_csv(RESULTS / "asymmetry.csv", index=False)
    lead = lead_lag(daily)
    lead.to_csv(RESULTS / "lead_lag.csv", index=False)
    annual = grouped_metrics(daily, ["year"])
    annual.to_csv(RESULTS / "annual_comparison.csv", index=False)
    regimes = grouped_metrics(daily[daily["vol_regime"] != "WARMUP"], ["period", "btc_direction", "vol_regime"])
    regimes.to_csv(RESULTS / "regime_comparison.csv", index=False)
    controls = control_regressions(daily)
    controls.to_csv(RESULTS / "control_regression.csv", index=False)
    structure = structural_change(daily)
    structure.to_csv(RESULTS / "structural_change.csv", index=False)
    pd.DataFrame([{
        "status": INTRADAY_STATUS, "horizons_minutes": "5,15,30,60",
        "reason": "No reliable 2020-latest timestamped MSTR intraday source was available; daily data was not substituted.",
    }]).to_csv(RESULTS / "intraday_lead_lag_status.csv", index=False)

    summary = {
        "generated_at_utc": generated_at,
        "all": corr_metrics(daily),
        "research_2020_2023": corr_metrics(daily[daily["period"] == "research_2020_2023"]),
        "holdout_2024_plus": corr_metrics(daily[daily["period"] == "holdout_2024_plus"]),
        "price_level_pearson": float(np.log(daily["close"]).corr(np.log(daily["btc_close"]))),
        "price_level_spearman": spearman_corr(np.log(daily["close"]), np.log(daily["btc_close"])),
        "important_boundary": "Price-level correlation and synchronous return relation are not predictive evidence.",
    }
    (RESULTS / "summary_metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    requested = int((pd.Timestamp(end_exclusive, tz="UTC") - pd.Timestamp(args.start, tz="UTC")).total_seconds() / BITSTAMP_STEP_SECONDS)
    btc_stamps = btc["candle_start_utc"].drop_duplicates().sort_values()
    gaps = btc_stamps.diff().dt.total_seconds().fillna(BITSTAMP_STEP_SECONDS)
    contiguous_expected = int((btc_stamps.max() - pd.Timestamp(args.start, tz="UTC")).total_seconds() / BITSTAMP_STEP_SECONDS) + 1
    internal_missing = max(0, contiguous_expected - len(btc_stamps))
    quality = {
        "generated_at_utc": generated_at, "tls_verification": not args.insecure_tls,
        "aligned_rows": len(daily), "first_aligned_date": daily["date"].min().date().isoformat(),
        "last_aligned_date": daily["date"].max().date().isoformat(),
        "point_in_time_all_pass": bool(daily[["open_point_in_time_pass", "close_point_in_time_pass"]].all().all()),
        "max_open_staleness_seconds": float(daily["btc_open_staleness_seconds"].max()),
        "max_close_staleness_seconds": float(daily["btc_close_staleness_seconds"].max()),
        "btc_requested_candles": requested, "btc_contiguous_expected_candles": contiguous_expected,
        "btc_rows": len(btc), "btc_internal_missing_candles": internal_missing,
        "btc_unavailable_tail_candles": max(0, requested - contiguous_expected),
        "btc_intervals_over_15m": int((gaps > BITSTAMP_STEP_SECONDS).sum()),
        "mstr_missing_core_values": int(mstr[["open", "high", "low", "close", "volume"]].isna().any(axis=1).sum()),
        "qqq_missing_core_values": int(qqq[["open", "high", "low", "close", "volume"]].isna().any(axis=1).sum()),
        "mstr_split": split_check,
    }
    if not quality["point_in_time_all_pass"]:
        raise AssertionError("point-in-time leakage check failed")
    (RESULTS / "data_quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / "source_manifest.json").write_text(
        json.dumps(manifest((mstr_src, mstr_actions_src, qqq_src, qqq_actions_src, btc_src), args.insecure_tls, generated_at), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    create_report(daily, summary, asym, lead, annual, regimes, controls, structure, quality)
    print(f"completed: {daily['date'].min().date()} through {daily['date'].max().date()}, rows={len(daily)}")


if __name__ == "__main__":
    main()
