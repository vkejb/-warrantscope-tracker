#!/usr/bin/env python3
"""Fixed-rule, offline MSTR bidirectional swing study. Never connects to a broker."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from urllib3.exceptions import InsecureRequestWarning


ROOT = Path(__file__).resolve().parent
LINKAGE = ROOT.parent / "mstr_btc_price_linkage_v01"
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "results"
PARAMS_PATH = ROOT / "params.json"
UTC = timezone.utc
VERSIONS = ("M1", "M2", "M3", "M4")
SHORT_VERSIONS = ("M2", "M3", "M4")


def ensure_dirs() -> None:
    for path in (RAW, PROCESSED, RESULTS):
        path.mkdir(parents=True, exist_ok=True)


def load_params() -> dict:
    return json.loads(PARAMS_PATH.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parameter_hash(params: dict) -> str:
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(canonical).hexdigest()


def request_json(session: requests.Session, url: str, params: dict, attempts: int = 6):
    delay = 0.8
    error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=45)
            if response.status_code in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:160]}")
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(delay)
            delay = min(12, delay * 2)
    raise RuntimeError(f"download failed: {error}")


def download_ibit(insecure_tls: bool) -> None:
    session = requests.Session()
    session.headers.update({"User-Agent": "WarrantScope-MSTR-Swing-Research/1.0"})
    session.verify = not insecure_tls
    if insecure_tls:
        warnings.simplefilter("ignore", InsecureRequestWarning)
    url = "https://query2.finance.yahoo.com/v8/finance/chart/IBIT"
    start = int(pd.Timestamp("2020-01-01", tz="UTC").timestamp())
    end = int((pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=1)).timestamp())
    payload = request_json(session, url, {"period1": start, "period2": end, "interval": "1d", "events": "div,splits"})
    result = payload.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"IBIT Yahoo payload error: {payload.get('chart', {}).get('error')}")
    chart = result[0]
    quote = chart["indicators"]["quote"][0]
    adjusted = chart["indicators"]["adjclose"][0]["adjclose"]
    rows = []
    for i, stamp in enumerate(chart["timestamp"]):
        row = {
            "timestamp_utc": pd.to_datetime(stamp, unit="s", utc=True),
            "date": pd.to_datetime(stamp, unit="s", utc=True).tz_convert("America/New_York").date().isoformat(),
        }
        for col in ("open", "high", "low", "close", "volume"):
            row[col] = quote[col][i]
        row["adj_close"] = adjusted[i]
        rows.append(row)
    pd.DataFrame(rows).sort_values("date").drop_duplicates("date").to_csv(RAW / "ibit_daily.csv", index=False)
    actions = []
    for kind, entries in chart.get("events", {}).items():
        for event in entries.values():
            stamp = int(event["date"])
            actions.append({
                "timestamp_utc": pd.to_datetime(stamp, unit="s", utc=True),
                "date_ny": pd.to_datetime(stamp, unit="s", utc=True).tz_convert("America/New_York").date().isoformat(),
                "event_type": kind, "split_ratio": event.get("splitRatio"),
                "numerator": event.get("numerator"), "denominator": event.get("denominator"), "amount": event.get("amount"),
            })
    pd.DataFrame(actions, columns=[
        "timestamp_utc", "date_ny", "event_type", "split_ratio", "numerator", "denominator", "amount"
    ]).to_csv(RAW / "ibit_corporate_actions.csv", index=False)
    (RAW / "ibit_download_metadata.json").write_text(json.dumps({
        "downloaded_at_utc": datetime.now(UTC).isoformat(), "source_url": url,
        "tls_verification": not insecure_tls, "requested_start": "2020-01-01",
        "first_returned_date": rows[0]["date"], "last_returned_date": rows[-1]["date"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def last_known_price(candles: pd.DataFrame, targets: pd.Series) -> pd.DataFrame:
    left = pd.DataFrame({"target": pd.to_datetime(targets, utc=True), "row_id": np.arange(len(targets))}).sort_values("target")
    right = candles[["candle_end_utc", "close"]].sort_values("candle_end_utc")
    out = pd.merge_asof(left, right, left_on="target", right_on="candle_end_utc", direction="backward")
    return out.sort_values("row_id").reset_index(drop=True)


def load_and_prepare(params: dict) -> tuple[pd.DataFrame, dict, list[dict]]:
    required = [
        LINKAGE / "data/processed/daily_aligned.csv",
        LINKAGE / "data/raw/mstr_daily.csv",
        LINKAGE / "data/raw/mstr_corporate_actions.csv",
        LINKAGE / "data/raw/qqq_daily.csv",
        LINKAGE / "data/raw/bitstamp_btcusd_15m.csv",
        RAW / "ibit_daily.csv",
        RAW / "ibit_corporate_actions.csv",
        RAW / "ibit_download_metadata.json",
    ]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing source files: {missing}")

    aligned = pd.read_csv(LINKAGE / "data/processed/daily_aligned.csv", parse_dates=["date", "market_open", "market_close"])
    mstr = pd.read_csv(LINKAGE / "data/raw/mstr_daily.csv", parse_dates=["date"])
    qqq = pd.read_csv(LINKAGE / "data/raw/qqq_daily.csv", parse_dates=["date"])
    ibit = pd.read_csv(RAW / "ibit_daily.csv", parse_dates=["date"])
    btc = pd.read_csv(LINKAGE / "data/raw/bitstamp_btcusd_15m.csv", parse_dates=["candle_start_utc", "candle_end_utc"])

    base = mstr[["date", "open", "high", "low", "close", "adj_close", "volume"]].copy()
    base = base.merge(aligned[[
        "date", "market_open", "market_close", "btc_close", "btc_close_candle_end_utc", "btc_session_log_return"
    ]], on="date", how="inner", validate="one_to_one")
    base = base.merge(
        qqq[["date", "open", "close", "adj_close"]].rename(columns={"open": "qqq_open", "close": "qqq_close", "adj_close": "qqq_adj_close"}),
        on="date", how="left", validate="one_to_one",
    )
    base = base.merge(
        ibit[["date", "open", "close", "adj_close"]].rename(columns={"open": "ibit_open", "close": "ibit_close", "adj_close": "ibit_adj_close"}),
        on="date", how="left", validate="one_to_one",
    ).sort_values("date").reset_index(drop=True)

    btc_lookback_targets = base["market_close"] - pd.Timedelta(days=params["btc_confirmation_lookback_calendar_days"])
    btc_past = last_known_price(btc, btc_lookback_targets)
    base["btc_30d_reference_price"] = btc_past["close"]
    base["btc_30d_reference_candle_end_utc"] = btc_past["candle_end_utc"]
    base["btc_30d_return"] = base["btc_close"] / base["btc_30d_reference_price"] - 1.0

    prev_close = base["close"].shift(1)
    true_range = pd.concat([
        base["high"] - base["low"],
        (base["high"] - prev_close).abs(),
        (base["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    base["ma20"] = base["close"].rolling(params["ma_fast_days"], min_periods=params["ma_fast_days"]).mean()
    base["ma60"] = base["close"].rolling(params["ma_slow_days"], min_periods=params["ma_slow_days"]).mean()
    base["atr14"] = true_range.rolling(params["atr_days"], min_periods=params["atr_days"]).mean()
    base["prior20_high"] = base["high"].shift(1).rolling(params["breakout_days"], min_periods=params["breakout_days"]).max()
    base["prior20_low"] = base["low"].shift(1).rolling(params["breakout_days"], min_periods=params["breakout_days"]).min()
    base["long_signal"] = (base["ma20"] > base["ma60"]) & (base["close"] > base["prior20_high"])
    base["short_signal"] = (base["ma20"] < base["ma60"]) & (base["close"] < base["prior20_low"])
    base["m4_long_signal"] = base["long_signal"] & (base["btc_30d_return"] > 0)
    base["m4_short_signal"] = base["short_signal"] & (base["btc_30d_return"] < 0)
    base["mstr_log_return"] = np.log(base["close"] / base["close"].shift(1))
    base["qqq_log_return"] = np.log(base["qqq_adj_close"] / base["qqq_adj_close"].shift(1))
    base["period"] = np.where(base["date"] < pd.Timestamp(params["holdout_start"]), "research", "holdout")
    base.to_csv(PROCESSED / "research_daily.csv", index=False)

    actions = pd.read_csv(LINKAGE / "data/raw/mstr_corporate_actions.csv")
    split = actions[
        (pd.to_numeric(actions["numerator"], errors="coerce") == 10)
        & (pd.to_numeric(actions["denominator"], errors="coerce") == 1)
        & (actions["date_ny"] == "2024-08-08")
    ]
    if split.empty:
        raise AssertionError("MSTR 2024-08-08 10:1 split event missing")
    split_move = base.set_index("date").loc["2024-08-07":"2024-08-09", "close"].pct_change().abs().max()
    if split_move > 0.50:
        raise AssertionError(f"MSTR split continuity failed: {split_move}")
    if base["ibit_adj_close"].first_valid_index() is None:
        raise AssertionError("IBIT data absent")
    ibit_first = base.loc[base["ibit_adj_close"].first_valid_index(), "date"]
    if ibit_first != pd.Timestamp("2024-01-11") or base.loc[base["date"] < ibit_first, "ibit_adj_close"].notna().any():
        raise AssertionError(f"IBIT listing boundary failed: {ibit_first}")
    if not (pd.to_datetime(base["btc_close_candle_end_utc"], utc=True) <= pd.to_datetime(base["market_close"], utc=True)).all():
        raise AssertionError("BTC close uses a future candle")
    btc_reference_end = pd.to_datetime(base["btc_30d_reference_candle_end_utc"], utc=True)
    btc_reference_target = pd.to_datetime(btc_lookback_targets, utc=True)
    comparable_reference = btc_reference_end.notna() & btc_reference_target.notna()
    if not (btc_reference_end[comparable_reference] <= btc_reference_target[comparable_reference]).all():
        raise AssertionError("BTC 30-day reference uses a future candle")

    ibit_metadata = json.loads((RAW / "ibit_download_metadata.json").read_text(encoding="utf-8"))
    quality = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "aligned_rows": len(base), "first_date": base["date"].min().date().isoformat(), "last_date": base["date"].max().date().isoformat(),
        "research_rows": int((base["period"] == "research").sum()), "holdout_rows": int((base["period"] == "holdout").sum()),
        "ibit_first_date": ibit_first.date().isoformat(), "ibit_rows": int(base["ibit_adj_close"].notna().sum()),
        "ibit_prelisting_values": int(base.loc[base["date"] < ibit_first, "ibit_adj_close"].notna().sum()),
        "ibit_tls_verification": ibit_metadata["tls_verification"], "ibit_downloaded_at_utc": ibit_metadata["downloaded_at_utc"],
        "mstr_split_date": "2024-08-08", "mstr_split_ratio": "10:1", "split_window_max_abs_return": float(split_move),
        "btc_close_point_in_time_pass": True, "btc_30d_point_in_time_pass": True,
        "btc_30d_warmup_missing_rows": int(base["btc_30d_reference_price"].isna().sum()),
        "missing_mstr_ohlcv_rows": int(base[["open", "high", "low", "close", "volume"]].isna().any(axis=1).sum()),
        "missing_qqq_rows": int(base[["qqq_open", "qqq_adj_close"]].isna().any(axis=1).sum()),
        "short_borrow_data_status": params["short_data_status"],
        "short_reason": "No reliable historical daily locate, borrow availability, fee, recall, or forced-buy-in series was available.",
    }
    sources = []
    for name, path, url in [
        ("validated daily alignment", LINKAGE / "data/processed/daily_aligned.csv", "derived by mstr_btc_price_linkage_v01"),
        ("MSTR OHLCV", LINKAGE / "data/raw/mstr_daily.csv", "https://query2.finance.yahoo.com/v8/finance/chart/MSTR"),
        ("MSTR corporate actions", LINKAGE / "data/raw/mstr_corporate_actions.csv", "https://query2.finance.yahoo.com/v8/finance/chart/MSTR"),
        ("QQQ OHLCV", LINKAGE / "data/raw/qqq_daily.csv", "https://query2.finance.yahoo.com/v8/finance/chart/QQQ"),
        ("BTC/USD 15m", LINKAGE / "data/raw/bitstamp_btcusd_15m.csv", "https://www.bitstamp.net/api/v2/ohlc/btcusd/"),
        ("IBIT OHLCV", RAW / "ibit_daily.csv", "https://query2.finance.yahoo.com/v8/finance/chart/IBIT"),
        ("IBIT corporate actions", RAW / "ibit_corporate_actions.csv", "https://query2.finance.yahoo.com/v8/finance/chart/IBIT"),
        ("IBIT download metadata", RAW / "ibit_download_metadata.json", "local acquisition record"),
    ]:
        sources.append({"name": name, "path": str(path.relative_to(ROOT.parent)), "source_url": url, "bytes": path.stat().st_size, "sha256": sha256(path)})
    return base, quality, sources


def adverse_price(raw_price: float, action: str, adverse_fill: float) -> float:
    if action == "BUY":
        return raw_price * (1.0 + adverse_fill)
    if action == "SELL":
        return raw_price * (1.0 - adverse_fill)
    raise ValueError(action)


def mark_equity(cash: float, position: dict | None, price: float) -> float:
    if not position:
        return cash
    signed = position["shares"] if position["side"] == "LONG" else -position["shares"]
    return cash + signed * price


def entry_side(row: pd.Series, version: str) -> str | None:
    if version == "M1":
        return "LONG" if row["long_signal"] else None
    if version == "M2":
        return "SHORT" if row["short_signal"] else None
    if version == "M3":
        if row["long_signal"]:
            return "LONG"
        if row["short_signal"]:
            return "SHORT"
        return None
    if version == "M4":
        if row["m4_long_signal"]:
            return "LONG"
        if row["m4_short_signal"]:
            return "SHORT"
        return None
    raise ValueError(version)


def trend_invalid(row: pd.Series, side: str) -> bool:
    if side == "LONG":
        return bool(row["close"] < row["ma20"] or row["ma20"] <= row["ma60"])
    return bool(row["close"] > row["ma20"] or row["ma20"] >= row["ma60"])


def simulate_strategy(
    data: pd.DataFrame,
    version: str,
    params: dict,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    cost_multiplier: float = 1.0,
    adverse_fill: float = 0.0,
    borrow_rate: float | None = None,
    skip_entry: tuple[str, str] | None = None,
    skip_entry_window: tuple[str, str, str] | None = None,
    deny_short_entries: bool = False,
    force_short_recall_days: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    frame = data[(data["date"] >= start_date) & (data["date"] <= end_date)].copy().reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"empty sample {start_date} to {end_date}")
    initial = float(params["initial_equity_usd"])
    cash = initial
    position = None
    pending_entry = None
    pending_exit_reason = None
    trades, daily, skips = [], [], []
    cost_rate = params["transaction_cost_bps_per_side"] * cost_multiplier / 10000.0
    borrow_rate = params["baseline_short_borrow_rate_annual"] if borrow_rate is None else borrow_rate
    cumulative_cost = 0.0
    cumulative_borrow = 0.0

    def close_position(row: pd.Series, price: float, reason: str, row_number: int, open_execution: bool) -> None:
        nonlocal cash, position, cumulative_cost
        assert position is not None
        action = "SELL" if position["side"] == "LONG" else "BUY"
        fill = adverse_price(price, action, adverse_fill) if open_execution else price
        exit_notional = position["shares"] * fill
        exit_cost = exit_notional * cost_rate
        if position["side"] == "LONG":
            cash += exit_notional - exit_cost
            gross = (fill - position["entry_price"]) * position["shares"]
        else:
            cash -= exit_notional + exit_cost
            gross = (position["entry_price"] - fill) * position["shares"]
        cumulative_cost += exit_cost
        net = gross - position["entry_cost"] - exit_cost - position["borrow_cost"]
        actual_loss = max(0.0, -net)
        gap_overrun = max(0.0, actual_loss - position["risk_budget"]) if reason == "STOP_GAP" else 0.0
        trades.append({
            "version": version, "side": position["side"], "signal_date": position["signal_date"],
            "entry_date": position["entry_date"], "exit_date": row["date"], "entry_price": position["entry_price"],
            "exit_price": fill, "shares": position["shares"], "entry_notional": position["entry_notional"],
            "exit_notional": exit_notional, "entry_equity": position["entry_equity"],
            "stop_price": position["stop_price"], "planned_risk": position["planned_risk"],
            "risk_budget": position["risk_budget"], "gross_pnl": gross, "transaction_cost": position["entry_cost"] + exit_cost,
            "borrow_cost": position["borrow_cost"], "net_pnl": net, "return_on_entry_notional": net / position["entry_notional"],
            "holding_trading_days": row_number - position["entry_row"] + 1,
            "holding_calendar_days": (row["date"] - position["entry_date"]).days + 1,
            "exit_reason": reason, "overnight_gap_risk_overrun": gap_overrun,
            "risk_budget_exceeded": bool(actual_loss > position["risk_budget"] + 1e-9),
            "short_qualification": params["short_simulation_status"] if position["side"] == "SHORT" else "NOT_APPLICABLE",
        })
        position = None

    for i, row in frame.iterrows():
        costs_today = 0.0
        borrow_today = 0.0
        raw_open = float(row["open"])

        if pending_exit_reason and position is not None:
            before = cumulative_cost
            close_position(row, raw_open, pending_exit_reason, i, True)
            costs_today += cumulative_cost - before
        pending_exit_reason = None

        if pending_entry is not None and position is None:
            side, signal_date, signal_atr = pending_entry
            signature = (str(pd.Timestamp(signal_date).date()), side)
            window_blocked = False
            if skip_entry_window is not None:
                block_start, block_end, block_side = skip_entry_window
                signal_day = pd.Timestamp(signal_date).date()
                window_blocked = pd.Timestamp(block_start).date() <= signal_day <= pd.Timestamp(block_end).date() and side == block_side
            if side == "SHORT" and deny_short_entries:
                skips.append({"version": version, "signal_date": signal_date, "side": side, "reason": "SHORT_UNAVAILABLE_STRESS"})
            elif (skip_entry is not None and signature == skip_entry) or window_blocked:
                skips.append({"version": version, "signal_date": signal_date, "side": side, "reason": "BEST_TRADE_REMOVAL_STRESS"})
            else:
                action = "BUY" if side == "LONG" else "SELL"
                fill = adverse_price(raw_open, action, adverse_fill)
                equity_before = mark_equity(cash, None, fill)
                stop_distance = params["atr_stop_multiple"] * float(signal_atr)
                risk_budget = equity_before * params["risk_per_trade_fraction"]
                risk_shares = math.floor(risk_budget / stop_distance) if stop_distance > 0 else 0
                nominal_shares = math.floor(equity_before * params["max_notional_fraction"] / fill)
                margin_shares = math.floor(equity_before / (params["short_margin_requirement_fraction"] * fill)) if side == "SHORT" else nominal_shares
                shares = min(risk_shares, nominal_shares, margin_shares)
                if shares < 1:
                    skips.append({"version": version, "signal_date": signal_date, "side": side, "reason": "NO_LEGAL_WHOLE_SHARE_SIZE"})
                else:
                    notional = shares * fill
                    entry_cost = notional * cost_rate
                    if side == "LONG":
                        cash -= notional + entry_cost
                        stop = fill - stop_distance
                    else:
                        cash += notional - entry_cost
                        stop = fill + stop_distance
                    cumulative_cost += entry_cost
                    costs_today += entry_cost
                    position = {
                        "side": side, "shares": shares, "entry_price": fill, "entry_notional": notional,
                        "entry_cost": entry_cost, "entry_date": row["date"], "signal_date": signal_date,
                        "entry_row": i, "stop_price": stop, "planned_risk": shares * stop_distance,
                        "risk_budget": risk_budget, "entry_equity": equity_before, "borrow_cost": 0.0,
                    }
        pending_entry = None

        if position is not None:
            stop_gap = (position["side"] == "LONG" and raw_open <= position["stop_price"]) or (
                position["side"] == "SHORT" and raw_open >= position["stop_price"]
            )
            stop_touch = (position["side"] == "LONG" and float(row["low"]) <= position["stop_price"]) or (
                position["side"] == "SHORT" and float(row["high"]) >= position["stop_price"]
            )
            if stop_gap:
                before = cumulative_cost
                close_position(row, raw_open, "STOP_GAP", i, True)
                costs_today += cumulative_cost - before
            elif stop_touch:
                before = cumulative_cost
                close_position(row, float(position["stop_price"]), "STOP_INTRADAY", i, False)
                costs_today += cumulative_cost - before

        if position is not None and position["side"] == "SHORT":
            next_date = frame.iloc[i + 1]["date"] if i + 1 < len(frame) else row["date"] + pd.Timedelta(days=1)
            calendar_days = max(1, int((next_date - row["date"]).days))
            borrow_today = position["shares"] * float(row["close"]) * borrow_rate * calendar_days / 365.0
            cash -= borrow_today
            position["borrow_cost"] += borrow_today
            cumulative_borrow += borrow_today

        equity = mark_equity(cash, position, float(row["close"]))
        daily.append({
            "date": row["date"], "version": version, "equity": equity, "cash": cash,
            "position_side": position["side"] if position else "FLAT", "shares": position["shares"] if position else 0,
            "gross_notional": position["shares"] * float(row["close"]) if position else 0.0,
            "gross_exposure_fraction": (position["shares"] * float(row["close"]) / equity) if position and equity > 0 else 0.0,
            "transaction_cost_today": costs_today, "borrow_cost_today": borrow_today,
            "cumulative_transaction_cost": cumulative_cost, "cumulative_borrow_cost": cumulative_borrow,
        })

        if position is not None:
            if trend_invalid(row, position["side"]):
                pending_exit_reason = "TREND_NEXT_OPEN"
            elif daily[-1]["gross_exposure_fraction"] > params["max_notional_fraction"]:
                pending_exit_reason = "NOTIONAL_CAP_NEXT_OPEN"
            elif position["side"] == "SHORT" and force_short_recall_days is not None:
                held = i - position["entry_row"] + 1
                if held >= force_short_recall_days:
                    pending_exit_reason = "SHORT_RECALL_STRESS"
        else:
            side = entry_side(row, version)
            if side and pd.notna(row["atr14"]):
                pending_entry = (side, row["date"], row["atr14"])

    if position is not None:
        row = frame.iloc[-1]
        before = cumulative_cost
        close_position(row, float(row["close"]), "END_OF_SAMPLE", len(frame) - 1, False)
        extra_cost = cumulative_cost - before
        daily[-1]["transaction_cost_today"] += extra_cost
        daily[-1]["cumulative_transaction_cost"] = cumulative_cost
        daily[-1]["equity"] = cash
        daily[-1]["cash"] = cash
        daily[-1]["position_side"] = "FLAT"
        daily[-1]["shares"] = 0
        daily[-1]["gross_notional"] = 0.0
        daily[-1]["gross_exposure_fraction"] = 0.0

    return pd.DataFrame(daily), pd.DataFrame(trades), pd.DataFrame(skips)


def simulate_buyhold(data: pd.DataFrame, name: str, open_col: str, close_col: str, start_date: pd.Timestamp,
                     end_date: pd.Timestamp, params: dict, allocation: float, fractional: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = data[(data["date"] >= start_date) & (data["date"] <= end_date) & data[close_col].notna()].copy().reset_index(drop=True)
    if frame.empty:
        raise ValueError(f"no data for {name}")
    initial = float(params["initial_equity_usd"])
    cost_rate = params["transaction_cost_bps_per_side"] / 10000.0
    entry = float(frame.iloc[0][open_col] if pd.notna(frame.iloc[0][open_col]) else frame.iloc[0][close_col])
    raw_shares = initial * allocation / (entry * (1.0 + cost_rate))
    shares = raw_shares if fractional else math.floor(raw_shares)
    entry_notional = shares * entry
    entry_cost = entry_notional * cost_rate
    cash = initial - entry_notional - entry_cost
    rows = []
    for i, row in frame.iterrows():
        equity = cash + shares * float(row[close_col])
        rows.append({
            "date": row["date"], "version": name, "equity": equity, "cash": cash,
            "position_side": "LONG", "shares": shares, "gross_notional": shares * float(row[close_col]),
            "gross_exposure_fraction": shares * float(row[close_col]) / equity,
            "transaction_cost_today": entry_cost if i == 0 else 0.0, "borrow_cost_today": 0.0,
            "cumulative_transaction_cost": entry_cost, "cumulative_borrow_cost": 0.0,
        })
    exit_price = float(frame.iloc[-1][close_col])
    exit_notional = shares * exit_price
    exit_cost = exit_notional * cost_rate
    cash += exit_notional - exit_cost
    rows[-1].update({
        "equity": cash, "cash": cash, "position_side": "FLAT", "shares": 0,
        "gross_notional": 0.0, "gross_exposure_fraction": 0.0,
        "transaction_cost_today": rows[-1]["transaction_cost_today"] + exit_cost,
        "cumulative_transaction_cost": entry_cost + exit_cost,
    })
    trade = pd.DataFrame([{
        "version": name, "side": "LONG", "signal_date": frame.iloc[0]["date"], "entry_date": frame.iloc[0]["date"],
        "exit_date": frame.iloc[-1]["date"], "entry_price": entry, "exit_price": exit_price, "shares": shares,
        "entry_notional": entry_notional, "exit_notional": exit_notional, "entry_equity": initial,
        "stop_price": np.nan,
        "planned_risk": np.nan, "risk_budget": np.nan, "gross_pnl": (exit_price - entry) * shares,
        "transaction_cost": entry_cost + exit_cost, "borrow_cost": 0.0,
        "net_pnl": (exit_price - entry) * shares - entry_cost - exit_cost,
        "return_on_entry_notional": ((exit_price - entry) * shares - entry_cost - exit_cost) / entry_notional,
        "holding_trading_days": len(frame), "holding_calendar_days": (frame.iloc[-1]["date"] - frame.iloc[0]["date"]).days + 1,
        "exit_reason": "END_OF_SAMPLE", "overnight_gap_risk_overrun": 0.0, "risk_budget_exceeded": False,
        "short_qualification": "NOT_APPLICABLE",
    }])
    return pd.DataFrame(rows), trade


def daily_returns(daily: pd.DataFrame, initial: float) -> pd.Series:
    returns = daily["equity"].pct_change()
    if len(returns):
        returns.iloc[0] = daily.iloc[0]["equity"] / initial - 1.0
    return returns.fillna(0.0)


def performance_metrics(daily: pd.DataFrame, trades: pd.DataFrame, initial: float) -> dict:
    returns = daily_returns(daily, initial)
    years = max((daily["date"].iloc[-1] - daily["date"].iloc[0]).days / 365.25, 1 / 365.25)
    final = float(daily["equity"].iloc[-1])
    total_return = final / initial - 1.0
    cagr = (final / initial) ** (1 / years) - 1.0 if final > 0 else -1.0
    std = returns.std(ddof=1)
    sharpe = returns.mean() / std * np.sqrt(252) if std > 0 else np.nan
    downside = returns[returns < 0]
    downside_std = np.sqrt((downside ** 2).mean()) if len(downside) else np.nan
    sortino = returns.mean() / downside_std * np.sqrt(252) if downside_std and downside_std > 0 else np.nan
    drawdown = daily["equity"] / daily["equity"].cummax() - 1.0
    annual = returns.groupby(daily["date"].dt.year).apply(lambda x: (1 + x).prod() - 1)
    wins = trades[trades["net_pnl"] > 0] if len(trades) else trades
    losses = trades[trades["net_pnl"] < 0] if len(trades) else trades
    gross_profit = wins["net_pnl"].sum() if len(wins) else 0.0
    gross_loss = -losses["net_pnl"].sum() if len(losses) else 0.0
    return {
        "start_date": daily["date"].iloc[0].date().isoformat(), "end_date": daily["date"].iloc[-1].date().isoformat(),
        "days": len(daily), "years": years, "ending_equity": final, "net_pnl": final - initial,
        "total_return": total_return, "cagr": cagr, "annualized_volatility": std * np.sqrt(252),
        "sharpe": sharpe, "sortino": sortino, "profit_factor": gross_profit / gross_loss if gross_loss > 0 else np.nan,
        "win_rate": len(wins) / len(trades) if len(trades) else np.nan, "trade_count": len(trades),
        "max_drawdown": float(drawdown.min()), "average_win": wins["net_pnl"].mean() if len(wins) else np.nan,
        "average_loss": losses["net_pnl"].mean() if len(losses) else np.nan,
        "average_holding_trading_days": trades["holding_trading_days"].mean() if len(trades) else np.nan,
        "long_net_pnl": trades.loc[trades["side"] == "LONG", "net_pnl"].sum() if len(trades) else 0.0,
        "short_net_pnl": trades.loc[trades["side"] == "SHORT", "net_pnl"].sum() if len(trades) else 0.0,
        "turnover_total": (trades["entry_notional"].sum() + trades["exit_notional"].sum()) / daily["equity"].mean() if len(trades) else 0.0,
        "turnover_annualized": ((trades["entry_notional"].sum() + trades["exit_notional"].sum()) / daily["equity"].mean() / years) if len(trades) else 0.0,
        "cash_time_fraction": float((daily["position_side"] == "FLAT").mean()),
        "transaction_cost": float(trades["transaction_cost"].sum()) if len(trades) else 0.0,
        "borrow_cost": float(trades["borrow_cost"].sum()) if len(trades) else 0.0,
        "worst_year": int(annual.idxmin()), "worst_year_return": float(annual.min()),
        "worst_trade": float(trades["net_pnl"].min()) if len(trades) else np.nan,
        "gap_overrun_count": int((trades["overnight_gap_risk_overrun"] > 0).sum()) if len(trades) else 0,
        "gap_overrun_total": float(trades["overnight_gap_risk_overrun"].sum()) if len(trades) else 0.0,
        "gap_overrun_max": float(trades["overnight_gap_risk_overrun"].max()) if len(trades) else 0.0,
        "max_entry_notional_fraction": float((trades["entry_notional"] / trades["entry_equity"]).max()) if len(trades) else 0.0,
        "max_planned_risk_fraction": float((trades["planned_risk"] / trades["entry_equity"]).max()) if len(trades) and trades["planned_risk"].notna().any() else np.nan,
        "max_mark_to_market_exposure_fraction": float(daily["gross_exposure_fraction"].max()),
    }


def metrics_from_return_subset(returns: pd.Series, dates: pd.Series, initial: float) -> dict:
    equity = initial * (1 + returns).cumprod()
    years = max((dates.iloc[-1] - dates.iloc[0]).days / 365.25, 1 / 365.25)
    std = returns.std(ddof=1)
    downside = returns[returns < 0]
    downside_std = np.sqrt((downside ** 2).mean()) if len(downside) else np.nan
    dd = equity / equity.cummax() - 1
    return {
        "ending_equity": float(equity.iloc[-1]), "net_pnl": float(equity.iloc[-1] - initial),
        "total_return": float(equity.iloc[-1] / initial - 1),
        "cagr": float((equity.iloc[-1] / initial) ** (1 / years) - 1) if equity.iloc[-1] > 0 else -1.0,
        "sharpe": float(returns.mean() / std * np.sqrt(252)) if std > 0 else np.nan,
        "sortino": float(returns.mean() / downside_std * np.sqrt(252)) if downside_std and downside_std > 0 else np.nan,
        "max_drawdown": float(dd.min()),
    }


def sample_ranges(data: pd.DataFrame, params: dict) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    return {
        "full": (data["date"].min(), data["date"].max()),
        "research": (data["date"].min(), pd.Timestamp(params["research_end"])),
        "holdout": (pd.Timestamp(params["holdout_start"]), data["date"].max()),
        "common_ibit": (pd.Timestamp("2024-01-11"), data["date"].max()),
    }


def moving_block_ci(delta: np.ndarray, params: dict) -> tuple[float, float, float]:
    delta = delta[np.isfinite(delta)]
    if len(delta) < params["bootstrap_block_days"]:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(params["bootstrap_seed"])
    block = params["bootstrap_block_days"]
    starts = np.arange(0, len(delta) - block + 1)
    estimates = np.empty(params["bootstrap_samples"])
    blocks_needed = math.ceil(len(delta) / block)
    for i in range(params["bootstrap_samples"]):
        chosen = rng.choice(starts, size=blocks_needed, replace=True)
        sample = np.concatenate([delta[s:s + block] for s in chosen])[:len(delta)]
        estimates[i] = sample.mean() * 252
    return float(delta.mean() * 252), float(np.quantile(estimates, 0.025)), float(np.quantile(estimates, 0.975))


def residual_diagnostic(data: pd.DataFrame, params: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    train = data[data["period"] == "research"][["mstr_log_return", "btc_session_log_return", "qqq_log_return"]].dropna()
    x = np.column_stack([np.ones(len(train)), train[["btc_session_log_return", "qqq_log_return"]].to_numpy()])
    beta = np.linalg.lstsq(x, train["mstr_log_return"].to_numpy(), rcond=None)[0]
    output = data[["date", "period", "mstr_log_return", "btc_session_log_return", "qqq_log_return"]].copy()
    output["fitted_from_research_coefficients"] = beta[0] + beta[1] * output["btc_session_log_return"] + beta[2] * output["qqq_log_return"]
    output["mstr_residual"] = output["mstr_log_return"] - output["fitted_from_research_coefficients"]
    for horizon in (1, 5, 20):
        future = pd.concat([output["mstr_residual"].shift(-step) for step in range(1, horizon + 1)], axis=1)
        output[f"future_residual_{horizon}d"] = future.sum(axis=1, min_count=horizon)
    research_resid = output.loc[output["period"] == "research", "mstr_residual"].dropna()
    cuts = research_resid.quantile([0.2, 0.4, 0.6, 0.8]).to_list()
    output["research_fixed_residual_quintile"] = pd.cut(
        output["mstr_residual"], [-np.inf, *cuts, np.inf], labels=["Q1_LOW", "Q2", "Q3", "Q4", "Q5_HIGH"]
    )
    rows = []
    for (period, bucket), group in output.groupby(["period", "research_fixed_residual_quintile"], observed=True):
        row = {"period": period, "residual_bucket": bucket, "n": len(group)}
        for horizon in (1, 5, 20):
            values = group[f"future_residual_{horizon}d"].dropna()
            row[f"mean_future_residual_{horizon}d"] = values.mean()
            row[f"median_future_residual_{horizon}d"] = values.median()
            row[f"positive_rate_{horizon}d"] = (values > 0).mean()
        rows.append(row)
    summary = pd.DataFrame(rows)
    summary["alpha_research"] = beta[0]
    summary["beta_btc_research"] = beta[1]
    summary["beta_qqq_research"] = beta[2]
    summary["diagnostic_only"] = True
    return output, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-ibit", action="store_true")
    parser.add_argument("--insecure-tls", action="store_true")
    args = parser.parse_args()
    ensure_dirs()
    params = load_params()
    if args.refresh_ibit or not (RAW / "ibit_daily.csv").exists():
        download_ibit(args.insecure_tls)
    data, quality, sources = load_and_prepare(params)
    p_hash = parameter_hash(params)
    (RESULTS / "parameter_hash.json").write_text(json.dumps({
        "parameter_sha256": p_hash, "canonical_parameters": params,
        "statement": "Parameters were fixed before inspecting strategy results; no grid search was run."
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / "data_quality.json").write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / "source_manifest.json").write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")

    ranges = sample_ranges(data, params)
    all_daily, all_trades, all_skips, perf_rows = [], [], [], []
    strategy_runs: dict[tuple[str, str], tuple[pd.DataFrame, pd.DataFrame]] = {}
    initial = params["initial_equity_usd"]
    for sample, (start, end) in ranges.items():
        for version in VERSIONS:
            daily, trades, skips = simulate_strategy(data, version, params, start, end)
            daily["sample"] = sample
            trades["sample"] = sample
            skips["sample"] = sample
            strategy_runs[(sample, version)] = (daily, trades)
            all_daily.append(daily); all_trades.append(trades); all_skips.append(skips)
            perf_rows.append({"sample": sample, "version": version, "comparison_basis": "fixed_strategy_risk", **performance_metrics(daily, trades, initial)})

        benchmarks = [
            ("M0_MSTR_BH", "open", "close", 1.0, False, "native_100pct"),
            ("M0_MSTR_RISK25", "open", "close", 0.25, False, "notional_cap_25pct"),
            ("QQQ_BH", "qqq_open", "qqq_adj_close", 1.0, False, "native_100pct"),
            ("QQQ_RISK25", "qqq_open", "qqq_adj_close", 0.25, False, "notional_cap_25pct"),
            ("BTC_BH", "btc_close", "btc_close", 1.0, True, "native_100pct"),
            ("BTC_RISK25", "btc_close", "btc_close", 0.25, True, "notional_cap_25pct"),
        ]
        if sample in ("holdout", "common_ibit"):
            benchmarks += [
                ("IBIT_BH", "ibit_open", "ibit_adj_close", 1.0, False, "native_100pct"),
                ("IBIT_RISK25", "ibit_open", "ibit_adj_close", 0.25, False, "notional_cap_25pct"),
            ]
        for name, open_col, close_col, allocation, fractional, basis in benchmarks:
            daily, trades = simulate_buyhold(data, name, open_col, close_col, start, end, params, allocation, fractional)
            daily["sample"] = sample; trades["sample"] = sample
            all_daily.append(daily); all_trades.append(trades)
            perf_rows.append({"sample": sample, "version": name, "comparison_basis": basis, **performance_metrics(daily, trades, initial)})

    daily_all = pd.concat(all_daily, ignore_index=True, sort=False)
    trades_all = pd.concat(all_trades, ignore_index=True, sort=False)
    skips_all = pd.concat(all_skips, ignore_index=True, sort=False) if all_skips else pd.DataFrame()
    performance = pd.DataFrame(perf_rows)
    daily_all.to_csv(RESULTS / "daily_equity.csv", index=False)
    trades_all.to_csv(RESULTS / "trades.csv", index=False)
    skips_all.to_csv(RESULTS / "skipped_entries.csv", index=False)
    performance.to_csv(RESULTS / "performance.csv", index=False)

    annual_rows = []
    for (sample, version), group in daily_all.groupby(["sample", "version"]):
        returns = daily_returns(group.reset_index(drop=True), initial)
        for year, values in returns.groupby(group.reset_index(drop=True)["date"].dt.year):
            annual_rows.append({"sample": sample, "version": version, "year": int(year), "return": (1 + values).prod() - 1,
                                "trading_days": len(values)})
    pd.DataFrame(annual_rows).to_csv(RESULTS / "annual_performance.csv", index=False)

    sensitivity_rows = []
    for sample in ("full", "holdout"):
        start, end = ranges[sample]
        for version in VERSIONS:
            base_daily, base_trades = strategy_runs[(sample, version)]
            scenarios = [
                ("double_transaction_cost", {"cost_multiplier": params["double_cost_multiplier"]}),
                ("adverse_next_open_fill_1pct", {"adverse_fill": params["adverse_next_open_fill_fraction"]}),
            ]
            if len(base_trades):
                best = base_trades.loc[base_trades["net_pnl"].idxmax()]
                scenarios.append(("remove_best_trade", {"skip_entry_window": (
                    str(pd.Timestamp(best["signal_date"]).date()), str(pd.Timestamp(best["exit_date"]).date()), best["side"]
                )}))
            if version in SHORT_VERSIONS:
                for rate in params["borrow_stress_rates_annual"]:
                    scenarios.append((f"borrow_rate_{int(rate*100)}pct", {"borrow_rate": rate}))
                scenarios.append(("short_unavailable_all_entries", {"deny_short_entries": True}))
                scenarios.append((f"short_recall_after_{params['short_recall_stress_holding_days']}d", {
                    "force_short_recall_days": params["short_recall_stress_holding_days"]
                }))
            for scenario, kwargs in scenarios:
                d, t, _ = simulate_strategy(data, version, params, start, end, **kwargs)
                sensitivity_rows.append({"sample": sample, "version": version, "scenario": scenario, **performance_metrics(d, t, initial)})
            base_returns = daily_returns(base_daily.reset_index(drop=True), initial)
            base_dates = base_daily.reset_index(drop=True)["date"]
            annual = base_returns.groupby(base_dates.dt.year).apply(lambda x: (1 + x).prod() - 1)
            best_year = int(annual.idxmax())
            keep = base_dates.dt.year != best_year
            removed = metrics_from_return_subset(base_returns[keep].reset_index(drop=True), base_dates[keep].reset_index(drop=True), initial)
            sensitivity_rows.append({"sample": sample, "version": version, "scenario": f"remove_best_year_{best_year}", **removed})
    sensitivity = pd.DataFrame(sensitivity_rows)
    sensitivity.to_csv(RESULTS / "sensitivity.csv", index=False)

    incremental_rows = []
    for sample in ranges:
        m3d, _ = strategy_runs[(sample, "M3")]
        m4d, _ = strategy_runs[(sample, "M4")]
        r3 = daily_returns(m3d.reset_index(drop=True), initial).to_numpy()
        r4 = daily_returns(m4d.reset_index(drop=True), initial).to_numpy()
        mean_delta, lo, hi = moving_block_ci(r4 - r3, params)
        p3 = performance[(performance["sample"] == sample) & (performance["version"] == "M3")].iloc[0]
        p4 = performance[(performance["sample"] == sample) & (performance["version"] == "M4")].iloc[0]
        incremental_rows.append({
            "sample": sample, "delta_cagr_m4_minus_m3": p4["cagr"] - p3["cagr"],
            "delta_sharpe": p4["sharpe"] - p3["sharpe"], "delta_max_drawdown": p4["max_drawdown"] - p3["max_drawdown"],
            "annualized_mean_daily_return_delta": mean_delta, "block_bootstrap_95pct_lower": lo,
            "block_bootstrap_95pct_upper": hi, "bootstrap_samples": params["bootstrap_samples"],
            "block_days": params["bootstrap_block_days"], "incremental_value_supported": bool(lo > 0 and p4["cagr"] > p3["cagr"]),
        })
    incremental = pd.DataFrame(incremental_rows)
    incremental.to_csv(RESULTS / "m4_incremental_value.csv", index=False)

    m3_incremental_rows = []
    for sample in ranges:
        m1d, _ = strategy_runs[(sample, "M1")]
        m3d, _ = strategy_runs[(sample, "M3")]
        r1 = daily_returns(m1d.reset_index(drop=True), initial).to_numpy()
        r3 = daily_returns(m3d.reset_index(drop=True), initial).to_numpy()
        mean_delta, lo, hi = moving_block_ci(r3 - r1, params)
        p1 = performance[(performance["sample"] == sample) & (performance["version"] == "M1")].iloc[0]
        p3 = performance[(performance["sample"] == sample) & (performance["version"] == "M3")].iloc[0]
        m3_incremental_rows.append({
            "sample": sample, "delta_cagr_m3_minus_m1": p3["cagr"] - p1["cagr"],
            "delta_sharpe": p3["sharpe"] - p1["sharpe"], "delta_max_drawdown": p3["max_drawdown"] - p1["max_drawdown"],
            "annualized_mean_daily_return_delta": mean_delta, "block_bootstrap_95pct_lower": lo,
            "block_bootstrap_95pct_upper": hi, "bootstrap_samples": params["bootstrap_samples"],
            "block_days": params["bootstrap_block_days"], "incremental_value_supported": bool(lo > 0 and p3["cagr"] > p1["cagr"]),
        })
    m3_incremental = pd.DataFrame(m3_incremental_rows)
    m3_incremental.to_csv(RESULTS / "m3_incremental_value.csv", index=False)

    residual_daily, residual_summary = residual_diagnostic(data, params)
    residual_daily.to_csv(RESULTS / "residual_daily.csv", index=False)
    residual_summary.to_csv(RESULTS / "residual_diagnostic.csv", index=False)

    # Fixed 10% research-vol normalization; scale down only, never lever up.
    normalized_rows = []
    research_curves = daily_all[daily_all["sample"] == "research"]
    for version, group in research_curves.groupby("version"):
        group = group.reset_index(drop=True)
        ret = daily_returns(group, initial)
        vol = ret.std(ddof=1) * np.sqrt(252)
        multiplier = min(1.0, params["risk_normalization_target_volatility"] / vol) if vol > 0 else 0.0
        for sample in ("research", "holdout"):
            target = daily_all[(daily_all["sample"] == sample) & (daily_all["version"] == version)].reset_index(drop=True)
            if target.empty:
                continue
            target_ret = daily_returns(target, initial) * multiplier
            metrics = metrics_from_return_subset(target_ret, target["date"], initial)
            normalized_rows.append({"version": version, "sample": sample, "research_volatility": vol,
                                    "return_multiplier_no_leverage": multiplier, **metrics})
    pd.DataFrame(normalized_rows).to_csv(RESULTS / "risk_normalized_performance.csv", index=False)

    generate_report(data, performance, sensitivity, incremental, m3_incremental, residual_summary, quality, params, p_hash)
    print(f"completed rows={len(data)}, through={data['date'].max().date()}, parameter_hash={p_hash}")


def pct(value) -> str:
    return "NA" if pd.isna(value) else f"{value * 100:.2f}%"


def num(value, digits=2) -> str:
    return "NA" if pd.isna(value) else f"{value:.{digits}f}"


def generate_report(data: pd.DataFrame, perf: pd.DataFrame, sensitivity: pd.DataFrame, incremental: pd.DataFrame,
                    m3_incremental: pd.DataFrame, residual: pd.DataFrame, quality: dict, params: dict, p_hash: str) -> None:
    hold = perf[(perf["sample"] == "holdout")].set_index("version")
    full = perf[(perf["sample"] == "full")].set_index("version")
    common = perf[(perf["sample"] == "common_ibit")].set_index("version")
    inc_hold = incremental[incremental["sample"] == "holdout"].iloc[0]
    m3_inc_hold = m3_incremental[m3_incremental["sample"] == "holdout"].iloc[0]
    m1 = hold.loc["M1"]; m2 = hold.loc["M2"]; m3 = hold.loc["M3"]; m4 = hold.loc["M4"]
    stress_gate = {}
    for version in VERSIONS:
        rows = sensitivity[(sensitivity["sample"] == "holdout") & (sensitivity["version"] == version)]
        required = rows[rows["scenario"].str.contains("double_transaction_cost|adverse_next_open_fill|remove_best_trade|remove_best_year", regex=True)]
        stress_gate[version] = bool(len(required) and (required["total_return"] > params["robust_gate"]["stress_total_return_min"]).all())
    m1_gate = bool(m1["cagr"] > params["robust_gate"]["holdout_cagr_min"] and m1["sharpe"] > params["robust_gate"]["holdout_sharpe_min"]
                   and m1["max_drawdown"] > params["robust_gate"]["holdout_max_drawdown_floor"] and stress_gate["M1"])
    m4_incremental = bool(inc_hold["incremental_value_supported"])
    qualified = [v for v, row in (("M1", m1),) if m1_gate]
    overall_status = "ELIGIBLE_FOR_PROSPECTIVE_PAPER_SHADOW" if qualified else "NO_ROBUST_EDGE"
    short_status = params["short_data_status"]
    long_vs_short = "多頭" if m1["net_pnl"] > m2["net_pnl"] else "空頭（僅條件式）"
    m3_better_m1 = bool(m3_inc_hold["incremental_value_supported"])
    m3_better_bh = m3["sharpe"] > hold.loc["M0_MSTR_BH", "sharpe"] and m3["max_drawdown"] > hold.loc["M0_MSTR_BH", "max_drawdown"]
    mstr_common = common.loc["M0_MSTR_BH"]; ibit_common = common.loc["IBIT_BH"]; btc_common = common.loc["BTC_BH"]
    extra_worth = (mstr_common["sharpe"] > max(ibit_common["sharpe"], btc_common["sharpe"])
                   and abs(mstr_common["max_drawdown"]) <= 1.25 * max(abs(ibit_common["max_drawdown"]), abs(btc_common["max_drawdown"])))
    low_hold = residual[(residual["period"] == "holdout") & (residual["residual_bucket"] == "Q1_LOW")].iloc[0]
    high_hold = residual[(residual["period"] == "holdout") & (residual["residual_bucket"] == "Q5_HIGH")].iloc[0]
    max_strategy_exposure = max(full.loc[v, "max_mark_to_market_exposure_fraction"] for v in VERSIONS)
    stress_highlights = []
    for version in ("M1", "M3", "M4"):
        rows = sensitivity[(sensitivity["sample"] == "holdout") & (sensitivity["version"] == version)].set_index("scenario")
        best_year_row = rows[rows.index.str.startswith("remove_best_year_")].iloc[0]
        stress_highlights.append(
            f"| {version} | {pct(rows.loc['double_transaction_cost', 'total_return'])} | "
            f"{pct(rows.loc['adverse_next_open_fill_1pct', 'total_return'])} | "
            f"{pct(rows.loc['remove_best_trade', 'total_return'])} | {pct(best_year_row['total_return'])} |"
        )
    table_lines = ["| 版本 | CAGR | Sharpe | 最大回撤 | 淨損益 | 交易數 | 多頭貢獻 | 空頭貢獻 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for version in ("M0_MSTR_BH", "M1", "M2", "M3", "M4"):
        r = hold.loc[version]
        table_lines.append(f"| {version} | {pct(r['cagr'])} | {num(r['sharpe'])} | {pct(r['max_drawdown'])} | ${r['net_pnl']:,.0f} | {int(r['trade_count'])} | ${r['long_net_pnl']:,.0f} | ${r['short_net_pnl']:,.0f} |")
    common_lines = ["| 資產 | CAGR | 年化波動 | Sharpe | 最大回撤 |", "|---|---:|---:|---:|---:|"]
    for version in ("M0_MSTR_BH", "IBIT_BH", "BTC_BH", "QQQ_BH"):
        r = common.loc[version]
        common_lines.append(f"| {version} | {pct(r['cagr'])} | {pct(r['annualized_volatility'])} | {num(r['sharpe'])} | {pct(r['max_drawdown'])} |")
    period_lines = ["| 版本 | 研究期 CAGR | 保留期 CAGR | 研究期 Sharpe | 保留期 Sharpe |", "|---|---:|---:|---:|---:|"]
    research_perf = perf[perf["sample"] == "research"].set_index("version")
    for version in VERSIONS:
        rr = research_perf.loc[version]; hh = hold.loc[version]
        period_lines.append(f"| {version} | {pct(rr['cagr'])} | {pct(hh['cagr'])} | {num(rr['sharpe'])} | {num(hh['sharpe'])} |")
    report = f"""# MSTR 多空雙向波段研究（繁中報告）

資料期間：{data['date'].min().date()}～{data['date'].max().date()}

固定參數 SHA-256：`{p_hash}`

最終研究狀態：**{overall_status}**

SHORT 資格狀態：**{short_status} / {params['short_simulation_status']}**

## 先回答六個問題

1. **MSTR 自身多頭與空頭趨勢哪邊有獨立收益？** 保留期 M1 淨損益 ${m1['net_pnl']:,.0f}、CAGR {pct(m1['cagr'])}；M2 在扣除基準年化 {pct(params['baseline_short_borrow_rate_annual'])} 借券費後淨損益 ${m2['net_pnl']:,.0f}、CAGR {pct(m2['cagr'])}。較強的是 **{long_vs_short}**。M2 因缺少歷史 locate/recall 證據，不能宣稱可實盤複製。
2. **M3 是否優於只做多及買進持有？** M3 對 M1 的保留期 CAGR 差 {pct(m3_inc_hold['delta_cagr_m3_minus_m1'])}、Sharpe 差 {num(m3_inc_hold['delta_sharpe'])}，block-bootstrap 95% CI [{pct(m3_inc_hold['block_bootstrap_95pct_lower'])}, {pct(m3_inc_hold['block_bootstrap_95pct_upper'])}]，因此穩健增量判定為 **{'是' if m3_better_m1 else '否'}**；對 100% MSTR 買進持有的風險調整結果：{'是' if m3_better_bh else '否'}。M3 的空方增量還受 SHORT `INSUFFICIENT_DATA` 限制。
3. **M4 加 BTC 後是否真的改善？** M4-M3 保留期 CAGR 差 {pct(inc_hold['delta_cagr_m4_minus_m3'])}、Sharpe 差 {num(inc_hold['delta_sharpe'])}；20 日 moving-block bootstrap 的年化平均日報酬差 95% CI 為 [{pct(inc_hold['block_bootstrap_95pct_lower'])}, {pct(inc_hold['block_bootstrap_95pct_upper'])}]。結論：**{'有穩健增量價值' if m4_incremental else '沒有證明穩健增量價值'}**。
4. **MSTR 相較 IBIT/BTC 的額外波動值得嗎？** 掛牌後共同期間，MSTR/IBIT/BTC 年化波動分別為 {pct(mstr_common['annualized_volatility'])}/{pct(ibit_common['annualized_volatility'])}/{pct(btc_common['annualized_volatility'])}，Sharpe 分別為 {num(mstr_common['sharpe'])}/{num(ibit_common['sharpe'])}/{num(btc_common['sharpe'])}。依風險調整報酬判斷：**{'值得，但仍需前瞻驗證' if extra_worth else '沒有證據顯示額外波動值得承擔'}**。
5. **SHORT 扣借券成本及不可借風險後可行嗎？** 借券費已納入基準並測 5%/15%/30%，但逐日 locate、可借數量、召回及強制回補資料不存在，故只能回答 **{short_status}**，不能用條件式回測證明可行。
6. **哪個版本值得進 prospective paper shadow？** **{', '.join(qualified) if qualified else '無；本輪結案 NO_ROBUST_EDGE'}**。含 SHORT 的 M2/M3/M4 在借券資格資料補齊前不具 shadow 資格；M4 另需通過對 M3 的增量價值門檻。

## 保留期績效

{chr(10).join(table_lines)}

完整 CAGR、淨損益、Sharpe、Sortino、PF、勝率、最大回撤、平均勝敗、平均持有日、多空貢獻、換手率、空手時間、成本、最差年度/單筆與 gap overrun 均在 `results/performance.csv`。

## 規則、成交與風險

- MA20/MA60 趨勢；收盤突破/跌破「不含當日」的先前 20 日高低區間。
- 固定 2×ATR14 停損；多空完全對稱。趨勢失效於收盤判斷，最早下一 NYSE 開盤成交；不做同一開盤立即反轉。
- 單筆計畫風險不超過當時權益 0.5%，入場名目曝險不超過 25%，整股向下取整；無合法股數即跳過。若價格漂移使收盤曝險超過 25%，下一開盤全數退出；全期最高暫時 mark-to-market 曝險為 {pct(max_strategy_exposure)}。
- LONG/SHORT 不同時存在、不加碼、不使用額外槓桿。SHORT 另受 150% margin capacity 檢查。
- 基準單邊交易成本 10 bps；SHORT 基準借券費年化 10%，按跨週末日曆日計費。
- stop 被隔夜跳空穿越時，以開盤價成交並記錄超過預算風險；詳見逐筆交易欄位。

## 同期間與相同風險對照

IBIT 首筆為 2024-01-11，沒有掛牌前回填。共同期間原生 100% 買進持有：

{chr(10).join(common_lines)}

另提供 25% 初始名目曝險基準（`*_RISK25`）與固定 10% 年化波動目標的 `risk_normalized_performance.csv`。波動正規化只用研究期估計倍率、只向下縮放，禁止槓桿放大；IBIT 無 2020–2023 研究期，未偽造其研究期風險倍率。

## 研究期、保留期與結構變化

研究期固定為 2020–2023，保留期固定為 2024 起。`performance.csv` 分別重設相同初始資金回測，`annual_performance.csv` 報每年結果，避免用全期單一數字掩蓋結構差異。沒有為保留期結果改 MA、突破、ATR 或 BTC 窗口。

{chr(10).join(period_lines)}

M1/M3/M4 的 CAGR 與 Sharpe 均由研究期降到保留期；M4 在研究期略有差異，但保留期交易與 M3 完全相同。這是明顯的期間結構變化，也是不能以全期績效宣稱穩健的原因。

## 壓力測試

`sensitivity.csv` 包含：雙倍成本、所有 next-open 成交額外 1% 不利、移除最佳交易、移除最佳年度，以及含 SHORT 版本的 5%/15%/30% 年借券費、全部 short entry 無法借券、short 持有 5 個交易日後被召回。移除最佳交易採重新回測並封鎖原交易存續窗口的同向再進場；移除最佳年度則從日報酬序列排除該年度，兩者定義均固定且明示。這些壓力仍不能取代真實 locate/recall 資料。

| 版本 | 雙倍成本總報酬 | +1%不利成交 | 移除最佳交易 | 移除最佳年度 |
|---|---:|---:|---:|---:|
{chr(10).join(stress_highlights)}

M1 在移除最佳年度後轉負，M3/M4 雖多數壓力仍為正，但其空方增量 CI 未排除零且借券資格不足，這是最終不進 paper shadow 的主要原因。

## BTC 增量與殘差純診斷

M4 僅增加在決策收盤已知的 BTC/USD 過去 30 個日曆日方向，其他規則、風險與成本均不變。M4-M3 使用 5,000 次、固定 seed、20 日 moving-block bootstrap，結果在 `m4_incremental_value.csv`。

殘差診斷用 2020–2023 固定迴歸係數扣除同期 BTC 與 QQQ 影響，再按研究期固定 quintile 觀察未來 1/5/20 日殘差延續或反轉。它只輸出 `residual_diagnostic.csv`，**沒有新增任何交易規則**。

保留期最低殘差 quintile 的後續 1/20 日平均殘差為 {pct(low_hold['mean_future_residual_1d'])}/{pct(low_hold['mean_future_residual_20d'])}，最高 quintile 為 {pct(high_hold['mean_future_residual_1d'])}/{pct(high_hold['mean_future_residual_20d'])}；方向隨 horizon 改變且與研究期不一致，沒有穩定的延續或反轉結論。

## 資料品質與限制

- MSTR 10:1 split：2024-08-08；事件附近最大絕對日變動 {pct(quality['split_window_max_abs_return'])}，無機械式 -90% 跳變。
- NYSE/DST 與 BTC 24/7 對齊重用上一研究的已驗證資料；BTC 30 日參考 candle 同樣通過 no-future 檢查。
- IBIT 實際起始日：{quality['ibit_first_date']}，掛牌前非空值 {quality['ibit_prelisting_values']} 筆。
- SHORT 的理論借券費壓力不能替代逐日 locate/recall/forced buy-in；所有含 SHORT 結果均非實盤績效。
- 此研究未連線券商、未送單、未修改或啟停任何交易程式。

## 重跑

```bash
python3 run_study.py
python3 -m unittest discover -s tests -v
python3 verify_outputs.py
```
"""
    (ROOT / "REPORT_zh-TW.md").write_text(report, encoding="utf-8")
    (RESULTS / "research_status.json").write_text(json.dumps({
        "status": overall_status, "short_status": short_status, "eligible_versions": qualified,
        "m4_incremental_value_supported": m4_incremental,
        "warning": "Historical backtest only; SHORT versions are conditional and are not live-readiness evidence."
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
