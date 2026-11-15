"""Offline, descriptive next-session outcome audit for all archived early-ready signals."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path
from statistics import mean, median
import zipfile

from yuanta_intraday_shadow_v01.collector import sha256_file

from .chart_chip_selection import ACTIVE, CLOSED_CALENDAR_CORRECTIONS, _daily_bars
from .raw_signal_comparison import OUT as SOURCE_OUT


HERE = Path(__file__).resolve().parent
SPEC = HERE / "NEXT_SESSION_SIGNAL_SPEC.md"
OUT = HERE / "next_session_signal_results"
HORIZONS = (1, 3, 5)
HOLIDAY_CORRECTIONS = CLOSED_CALENDAR_CORRECTIONS | {"20261009"}


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _write(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise ValueError("empty output")
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _pct(end: float, start: float) -> float:
    if end <= 0 or start <= 0:
        raise ValueError("nonpositive close")
    return (end / start - 1) * 100


def _status(target: str, as_of: str, latest: str, target_bar: dict | None) -> str:
    if target > as_of:
        return "PENDING"
    if target > latest:
        return "MISSING_ARCHIVE"
    return "COMPLETE" if target_bar and target_bar["close"] > 0 else "MISSING_BAR"


def _summary(rows: list[dict], label: str, horizon: int) -> dict:
    selected = [r for r in rows if r["group"] == label and r["horizon_sessions"] == horizon]
    valid = [r for r in selected if r["status"] == "COMPLETE"]
    values = [float(r["signal_to_target_pct"]) for r in valid]
    cont = [float(r["close_to_target_pct"]) for r in valid if r["close_to_target_pct"] != ""]
    excess = [float(r["excess_vs_0050_pct_points"]) for r in valid
              if r["excess_vs_0050_pct_points"] != ""]
    return {"group": label, "horizon_sessions": horizon, "signal_count": len(selected),
            "complete_count": len(valid), "pending_count": sum(r["status"] == "PENDING" for r in selected),
            "missing_archive_count": sum(r["status"] == "MISSING_ARCHIVE" for r in selected),
            "missing_bar_count": sum(r["status"] == "MISSING_BAR" for r in selected),
            "up_count": sum(v > 0 for v in values), "flat_count": sum(v == 0 for v in values),
            "down_count": sum(v < 0 for v in values),
            "up_share": sum(v > 0 for v in values) / len(values) if values else "",
            "mean_signal_to_target_pct": mean(values) if values else "",
            "median_signal_to_target_pct": median(values) if values else "",
            "min_signal_to_target_pct": min(values) if values else "",
            "max_signal_to_target_pct": max(values) if values else "",
            "continuation_count": len(cont), "continuation_up_count": sum(v > 0 for v in cont),
            "mean_close_to_target_pct": mean(cont) if cont else "",
            "median_close_to_target_pct": median(cont) if cont else "",
            "excess_0050_count": len(excess),
            "mean_excess_0050_pct_points": mean(excess) if excess else "",
            "distinct_complete_dates": len({r["session_date"] for r in valid})}


def run(*, out: Path = OUT, as_of: str | None = None,
        source: Path = SOURCE_OUT / "all_raw_signals.csv", active_path: Path = ACTIVE) -> dict:
    as_of = as_of or datetime.now(timezone.utc).astimezone().date().strftime("%Y%m%d")
    if len(as_of) != 8 or not as_of.isdigit():
        raise ValueError("as_of must be YYYYMMDD")
    raw = _csv(source)
    if len(raw) != 365 or len({(r["session_date"], r["stock_id"]) for r in raw}) != len(raw):
        raise RuntimeError("original 365-signal identity changed")
    symbols = {r["stock_id"] for r in raw} | {"0050"}
    bars, active = _daily_bars(symbols, active_path)
    calendar = Path(active["trading_calendar"])
    actual_calendar_hash = sha256_file(calendar)
    calendar_hash_status = ("VERIFIED" if actual_calendar_hash == active["trading_calendar_sha256"]
                            else "CALENDAR_HASH_MISMATCH")
    sessions = [r["date"].replace("-", "") for r in _csv(calendar)
                if r["date"].replace("-", "") not in HOLIDAY_CORRECTIONS]
    if sessions != sorted(set(sessions)):
        raise RuntimeError("calendar not strictly ordered")
    indices = {d: i for i, d in enumerate(sessions)}
    archived_dates = {b["date"] for b in bars.get("0050", [])}
    if not archived_dates:
        raise RuntimeError("no 0050 daily archive")
    latest = max(archived_dates)
    by_symbol = {s: {b["date"]: b for b in v} for s, v in bars.items()}
    results = []
    for signal in raw:
        session, symbol = signal["session_date"], signal["stock_id"]
        if session not in indices or session > latest:
            raise RuntimeError(f"signal date not validated in calendar/archive: {session}")
        signal_close = by_symbol.get(symbol, {}).get(session, {}).get("close")
        entry = float(signal["entry_price"])
        if entry <= 0:
            raise RuntimeError(f"invalid entry proxy: {session} {symbol}")
        base = {"session_date": session, "stock_id": symbol, "signal_time": signal["signal_time"],
                "signal_entry_proxy": entry, "signal_day_close": signal_close or "",
                "original_rule_pass": signal["original_rule_pass"],
                "account_selected": signal["account_selected"],
                "original_daily_ready_rank": signal["original_daily_ready_rank"],
                "actual_orders": 0}
        groups = ["ALL_RAW"]
        if signal["original_rule_pass"] == "True":
            groups.append("ORIGINAL_RULE_PASS")
        if signal["account_selected"] == "True":
            groups.append("ACCOUNT_SELECTED")
        for horizon in HORIZONS:
            position = indices[session] + horizon
            if position >= len(sessions):
                raise RuntimeError("calendar does not extend through horizon")
            target = sessions[position]
            bar = by_symbol.get(symbol, {}).get(target)
            status = _status(target, as_of, latest, bar)
            target_close = bar["close"] if status == "COMPLETE" else None
            etf_start = by_symbol.get("0050", {}).get(session, {}).get("close")
            etf_end = by_symbol.get("0050", {}).get(target, {}).get("close")
            continuation = _pct(target_close, signal_close) if target_close and signal_close else None
            etf_return = _pct(etf_end, etf_start) if target_close and etf_start and etf_end else None
            record = {**base, "horizon_sessions": horizon, "target_date": target,
                      "status": status, "target_close": target_close or "",
                      "signal_to_target_pct": _pct(target_close, entry) if target_close else "",
                      "close_to_target_pct": continuation if continuation is not None else "",
                      "etf_0050_return_pct": etf_return if etf_return is not None else "",
                      "excess_vs_0050_pct_points": (continuation - etf_return
                                                     if continuation is not None and etf_return is not None else "")}
            for group in groups:
                results.append({"group": group, **record})
    summary_rows = [_summary(results, group, horizon) for group in
                    ("ALL_RAW", "ORIGINAL_RULE_PASS", "ACCOUNT_SELECTED") for horizon in HORIZONS]
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "outcomes.csv", results)
    _write(out / "summary.csv", summary_rows)
    manifest = {"analysis_id": "NEXT_SESSION_SIGNAL_AUDIT_V1", "as_of": as_of,
                "latest_verified_daily_date": latest, "raw_signal_count": len(raw),
                "source_sha256": sha256_file(source), "spec_sha256": sha256_file(SPEC),
                "active_inputs_sha256": sha256_file(active_path),
                "calendar_sha256": actual_calendar_hash,
                "calendar_expected_sha256": active["trading_calendar_sha256"],
                "calendar_hash_status": calendar_hash_status,
                "holiday_corrections": sorted(HOLIDAY_CORRECTIONS),
                "archive_count": len(active["archive_hashes"]),
                "actual_orders": 0,
                "reproduce": "PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.next_session_signal_audit --as-of YYYYMMDD --out NEW_DIR"}
    (out / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = ("# 後續交易日訊號結果（研究，不是實盤）\n\n"
              f"原始訊號 {len(raw)} 筆；日線封存最後有效日 {latest}；截至 {as_of}。"
              f"日曆核對：{calendar_hash_status}，未來目標日期暫定。\n\n"
              "| 母體 | T+交易日 | 有效/總數 | 上漲 | 平均變動% | 中位變動% | 待到期 | 缺封存 | 缺股價 |\n"
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for r in summary_rows:
        report += (f"| {r['group']} | {r['horizon_sessions']} | {r['complete_count']}/{r['signal_count']} "
                   f"| {r['up_count']} | {r['mean_signal_to_target_pct']} | {r['median_signal_to_target_pct']} "
                   f"| {r['pending_count']} | {r['missing_archive_count']} | {r['missing_bar_count']} |\n")
    report += ("\n變動率為訊號時的模擬進場價到未來收盤，非可成交損益；另見 outcomes.csv 的當日收盤到未來收盤及0050比較。"
               "未調整除權息，沒有費用或隔夜交易模型。同日訊號相關，不能以筆數當獨立樣本。\n")
    (out / "REPORT.md").write_text(report, encoding="utf-8")
    with zipfile.ZipFile(out / "next_session_signal_private.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ("REPORT.md", "summary.csv", "outcomes.csv", "run_manifest.json"):
            archive.write(out / name, arcname=name)
        archive.write(SPEC, arcname="NEXT_SESSION_SIGNAL_SPEC.md")
    return {"manifest": manifest, "summary": summary_rows}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(out=args.out, as_of=args.as_of)["manifest"], ensure_ascii=False))
