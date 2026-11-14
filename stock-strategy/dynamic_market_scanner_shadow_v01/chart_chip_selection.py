"""Frozen chart/chip candidate ablation; historical research, no broker path."""
from __future__ import annotations

from collections import defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from statistics import mean, median
import sys
import time
import zipfile

from paper_shadow_v01.runner import _load_market_context
from prospective_notifications_v01.chip_watch import OfficialNotReady, _parse, _request
from yuanta_intraday_shadow_v01.collector import canonical_bytes, sha256_file

from .causal_auction import _verified_selected_quotes
from .early_entry_backtest import DEFAULT_RESULTS_DIR as EARLY_DIR
from .signal_selection_rule_impact import _fill, _fixed_30m, current_opportunity
from .strict_replay import DEFAULT_RUNS_DIR, _simulate_exit, _write_csv


HERE = Path(__file__).resolve().parent
SPEC = HERE / "CHART_CHIP_SELECTION_SPEC.md"
DEFAULT_OUT = HERE / "chart_chip_selection_results"
ACTIVE = HERE.parent / "shadow_daily_runner/runtime/active_inputs.json"
VARIANTS = ("BASE", "CHART", "CHIP", "CHART_AND_CHIP")
SCENARIOS = ("BASE", "STRESS")
SOURCES = ("TWSE_INSTITUTIONAL", "TPEX_INSTITUTIONAL")
CLOSED_CALENDAR_CORRECTIONS = {"20261023", "20261026"}


def _load_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def prior_bars(bars: list[dict], session: str, expected_prior: str | None, count: int = 20):
    """Strictly exclude the signal session even if it exists in the archive."""
    eligible = [bar for bar in bars if bar["date"] < session]
    return (eligible[-count:] if expected_prior and len(eligible) >= count
            and eligible[-1]["date"] == expected_prior else None)


def chart_pass(bars: list[dict] | None):
    if bars is None or len(bars) < 20:
        return None
    closes = [bar["close"] for bar in bars]
    if any(price <= 0 for price in closes) or any(bar["volume"] < 0 for bar in bars):
        return None
    ma5, ma20 = mean(closes[-5:]), mean(closes)
    return closes[-1] > ma5 and ma5 > ma20


def chip_pass(rows: dict[str, dict] | None, symbol: str):
    if rows is None or symbol not in rows:
        return None
    row = rows[symbol]
    values = [row.get(name) for name in ("foreign", "investment_trust", "dealer")]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values) > 0


def gate(chart: bool | None, chip: bool | None, variant: str):
    if variant == "BASE":
        return True, "PASSED"
    values = ([chart] if variant == "CHART" else [chip] if variant == "CHIP"
              else [chart, chip])
    if any(value is None for value in values):
        return False, "DATA_UNKNOWN"
    return (all(values), "PASSED" if all(values) else "GATE_REJECTED")


def _daily_bars(symbols: set[str], active_path: Path = ACTIVE):
    active = json.loads(active_path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, dict]] = defaultdict(dict)
    for item in active["archive_hashes"]:
        archive = Path(item["path"])
        if sha256_file(archive) != item["sha256"]:
            raise RuntimeError(f"daily archive hash mismatch: {archive}")
        with zipfile.ZipFile(archive) as folder:
            names = [name for name in folder.namelist() if name.endswith(".csv")]
            if len(names) != 1:
                raise RuntimeError(f"unexpected daily archive format: {archive}")
            with folder.open(names[0]) as raw:
                for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                    symbol = row["code"]
                    if symbol not in symbols:
                        continue
                    bar = {"date": row["date"], "close": float(row["close"]),
                           "volume": float(row["volume"])}
                    old = result[symbol].get(bar["date"])
                    if old is not None and old != bar:
                        raise RuntimeError(f"conflicting daily bars: {symbol}:{bar['date']}")
                    result[symbol][bar["date"]] = bar
    return {symbol: sorted(dated.values(), key=lambda row: row["date"])
            for symbol, dated in result.items()}, active


def _previous_date(active: dict, session: str):
    with Path(active["trading_calendar"]).open(encoding="utf-8", newline="") as handle:
        sessions = [row["date"].replace("-", "") for row in csv.DictReader(handle)]
    dates = [date for date in sessions
             if date < session and date not in CLOSED_CALENDAR_CORRECTIONS]
    return dates[-1] if dates else None


def fetch_official(out: Path = DEFAULT_OUT, *, interval: float = 2.0):
    signals = _load_csv(EARLY_DIR / "signals.csv")
    dates = sorted({row["session_date"] for row in signals})
    symbols = {row["stock_id"] for row in signals}
    bars, _active = _daily_bars(symbols)
    previous = {day: _previous_date(_active, day) for day in dates}
    for date in sorted(set(previous.values()) - {None}):
        for source in SOURCES:
            folder = out / "official_reconstruction" / date / source
            raw_path, meta_path = folder / "raw.json", folder / "metadata.json"
            if raw_path.exists() and meta_path.exists():
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                if sha256_file(raw_path) != metadata["sha256"]:
                    raise RuntimeError(f"official raw hash mismatch: {raw_path}")
                if metadata["status"] != "OFFICIAL_NOT_READY":
                    _parse(source, date, raw_path.read_bytes(), symbols, metadata["url"])
                continue
            if raw_path.exists() or meta_path.exists():
                raise RuntimeError(f"partial official archive: {folder}")
            raw, url = _request(source, date)
            try:
                parsed = _parse(source, date, raw, symbols, url)
                issue = None
            except OfficialNotReady as exc:
                parsed, issue = None, str(exc)
            folder.mkdir(parents=True, exist_ok=True)
            metadata = {"source": source, "date": date, "url": url,
                        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "source_rows": parsed["source_row_count"] if parsed else 0,
                        "matched_rows": parsed["matched_row_count"] if parsed else 0,
                        "status": ("OFFICIAL_NOT_READY" if issue else
                                   "HISTORICAL_RECONSTRUCTION_NOT_PRESIGNAL_CAPTURE"),
                        "issue": issue}
            raw_path.write_bytes(raw)
            meta_path.write_bytes(canonical_bytes(metadata) + b"\n")
            time.sleep(interval)
    return previous


def _chips(out: Path, date: str | None, symbols: set[str]):
    if date is None:
        return None, "NO_PRIOR_SESSION"
    combined = {}
    for source in SOURCES:
        folder = out / "official_reconstruction" / date / source
        raw_path, meta_path = folder / "raw.json", folder / "metadata.json"
        if not raw_path.is_file() or not meta_path.is_file():
            return None, f"MISSING_{source}"
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        if metadata["date"] != date or sha256_file(raw_path) != metadata["sha256"]:
            raise RuntimeError(f"official source identity/hash changed: {folder}")
        if metadata["status"] == "OFFICIAL_NOT_READY":
            return None, f"OFFICIAL_NOT_READY_{source}"
        parsed = _parse(source, date, raw_path.read_bytes(), symbols, metadata["url"])
        for row in parsed["rows"]:
            symbol = row["security_code"]
            if symbol in combined:
                raise RuntimeError(f"cross-market duplicate security identity: {symbol}")
            combined[symbol] = row
    return combined, "HISTORICAL_RECONSTRUCTION"


def _metrics(trades: list[dict], daily: list[dict], variant: str, scenario: str):
    subset = [row for row in trades if row["variant"] == variant and row["scenario"] == scenario]
    selected = [row for row in subset if row["status"] != "NO_TRADE"]
    scored = [row for row in selected if row["status"] == "SCORED"]
    values = [float(row["net_pnl_twd"]) for row in scored]
    unknown = len(selected) - len(scored)
    wins, losses = [x for x in values if x > 0], [x for x in values if x < 0]
    day_rows = [row for row in daily if row["variant"] == variant and row["scenario"] == scenario]
    equity = peak = 190000.0
    drawdown = 0.0
    for row in day_rows:
        if row["net_pnl_twd"] is None:
            continue
        equity += float(row["net_pnl_twd"])
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {"variant": variant, "scenario": scenario, "dates": len(day_rows),
            "trades": len(selected), "scored": len(scored), "unscored": unknown,
            "trade_days": len({row["session_date"] for row in selected}),
            "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(values) if values else None,
            "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
            "scored_only_pnl_twd": sum(values),
            "account_pnl_twd": sum(values) if unknown == 0 else None,
            "mean_trade_twd": mean(values) if values else None,
            "median_trade_twd": median(values) if values else None,
            "max_drawdown_twd": drawdown if unknown == 0 else None,
            "worst_day_twd": min((float(row["net_pnl_twd"]) for row in day_rows
                                  if row["net_pnl_twd"] is not None), default=None),
            "worst_trade_twd": min(values) if values else None,
            "without_best_trade_twd": (sum(values) - max(values)) if values and unknown == 0 else None,
            "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}


def run(out: Path = DEFAULT_OUT):
    source_signals = _load_csv(EARLY_DIR / "signals.csv")
    dates = sorted({row["session_date"] for row in source_signals})
    symbols = {row["stock_id"] for row in source_signals}
    bars, active = _daily_bars(symbols)
    ledger, trades, daily, coverage = [], [], [], []
    for day in dates:
        source_date = _previous_date(active, day)
        chips, chip_status = _chips(out, source_date, symbols)
        cache = json.loads((EARLY_DIR / "replay_cache" / f"{day}.json").read_text(encoding="utf-8"))["replay"]
        run_dir = DEFAULT_RUNS_DIR / cache["source_run_id"]
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        if manifest["manifest_hash"] != cache["source_manifest_hash"]:
            raise RuntimeError(f"intraday source changed: {day}")
        raw = [row for row in source_signals if row["session_date"] == day]
        candidates = _verified_selected_quotes(run_dir, {row["stock_id"] for row in raw})
        market = _load_market_context(run_dir, manifest)
        session_eligible = []
        for row in raw:
            signal = dict(row)
            signal["signal_time"] = datetime.fromisoformat(row["signal_time"])
            signal["entry_price"] = float(row["entry_price"])
            signal["relative_strength_5m"] = float(row["relative_strength_5m"])
            outcome = current_opportunity(signal, candidates[row["stock_id"]])
            history = prior_bars(bars.get(row["stock_id"], []), day, source_date)
            chart = chart_pass(history)
            chip = chip_pass(chips, row["stock_id"])
            value = None
            if outcome["passed"]:
                fill, _ = _fill(outcome, candidates[row["stock_id"]], "BASE")
                value = _fixed_30m(signal, fill, candidates[row["stock_id"]]) if fill else None
                session_eligible.append((signal, outcome, chart, chip, value))
            for variant in VARIANTS:
                accepted, reason = gate(chart, chip, variant)
                ledger.append({"session_date": day, "stock_id": row["stock_id"],
                               "signal_time": row["signal_time"], "variant": variant,
                               "prior_source_date": source_date,
                               "prior_close": history[-1]["close"] if history else None,
                               "ma5": mean(x["close"] for x in history[-5:]) if history else None,
                               "ma20": mean(x["close"] for x in history) if history else None,
                               "chart_pass": chart, "chip_pass": chip,
                               "chip_net_shares": (sum(chips[row["stock_id"]][name] for name in
                                                      ("foreign", "investment_trust", "dealer"))
                                                   if chips and row["stock_id"] in chips else None),
                               "chip_evidence": chip_status,
                               "base_pass": outcome["passed"], "base_reason": outcome["reason"],
                               "gate_pass": accepted if outcome["passed"] else None,
                               "gate_reason": reason if outcome["passed"] else "BASE_NOT_READY",
                               "fixed_30m_net_pnl_twd": value,
                               "actual_orders": 0})
        coverage.append({"session_date": day, "prior_source_date": source_date,
                         "signal_count": len(raw), "base_eligible": len(session_eligible),
                         "chart_known": sum(item[2] is not None for item in session_eligible),
                         "chip_known": sum(item[3] is not None for item in session_eligible),
                         "chip_source_status": chip_status,
                         "intraday_manifest_hash": manifest["manifest_hash"]})
        for variant in VARIANTS:
            accepted = [item for item in session_eligible if gate(item[2], item[3], variant)[0]]
            accepted.sort(key=lambda item: (item[1]["effective_time"],
                                                  -item[0]["relative_strength_5m"], item[0]["stock_id"]))
            for scenario in SCENARIOS:
                if not accepted:
                    record = {"session_date": day, "variant": variant, "scenario": scenario,
                              "status": "NO_TRADE", "net_pnl_twd": 0.0, "actual_orders": 0}
                else:
                    signal, opportunity, _chart, _chip, _value = accepted[0]
                    fill, reason = _fill(opportunity, candidates[signal["stock_id"]], scenario)
                    if fill is None:
                        record = {"session_date": day, "variant": variant, "scenario": scenario,
                                  "stock_id": signal["stock_id"], "status": "UNFILLED",
                                  "reason": reason, "net_pnl_twd": None, "actual_orders": 0}
                    else:
                        selected = {**signal, "confirmation_time": fill["entry_time"],
                                    "delayed_entry_price": fill["entry_price"],
                                    "delayed_entry_quantity": fill["quantity"]}
                        trade = _simulate_exit(selected, {signal["stock_id"]: candidates[signal["stock_id"]]},
                                               market, {"session_date": day, "ended_at_taipei": manifest["ended_at"]})
                        record = {**trade, "variant": variant, "scenario": scenario,
                                  "status": "SCORED" if trade.get("net_pnl") is not None else trade["status"],
                                  "net_pnl_twd": trade.get("net_pnl")}
                trades.append(record)
                daily.append({"session_date": day, "variant": variant, "scenario": scenario,
                              "stock_id": record.get("stock_id"), "status": record["status"],
                              "net_pnl_twd": record["net_pnl_twd"], "actual_orders": 0})
    metrics = [_metrics(trades, daily, variant, scenario)
               for variant in VARIANTS for scenario in SCENARIOS]
    common_dates = {row["session_date"] for row in coverage
                    if row["chip_source_status"] == "HISTORICAL_RECONSTRUCTION"
                    and row["chart_known"] == row["base_eligible"]
                    and row["chip_known"] == row["base_eligible"]}
    fair = [_metrics([row for row in trades if row["session_date"] in common_dates],
                     [row for row in daily if row["session_date"] in common_dates],
                     variant, scenario)
            for variant in VARIANTS for scenario in SCENARIOS]
    for row in fair:
        row["scope"] = "COMMON_KNOWN_DATES_HISTORICAL_RECONSTRUCTION"
    changes = []
    for day in dates:
        for scenario in SCENARIOS:
            by_variant = {row["variant"]: row for row in trades
                          if row["session_date"] == day and row["scenario"] == scenario}
            base = by_variant["BASE"]
            for variant in VARIANTS[1:]:
                item = by_variant[variant]
                a, b = base["net_pnl_twd"], item["net_pnl_twd"]
                changes.append({"session_date": day, "scenario": scenario,
                                "variant": variant, "common_known_date": day in common_dates,
                                "base_stock_id": base.get("stock_id"),
                                "base_status": base["status"], "base_pnl_twd": a,
                                "variant_stock_id": item.get("stock_id"),
                                "variant_status": item["status"], "variant_pnl_twd": b,
                                "incremental_pnl_twd": (float(b) - float(a)
                                                        if a is not None and b is not None else None)})
    for variant in VARIANTS:
        for scenario in SCENARIOS:
            equity = peak = 190000.0
            unresolved = False
            for row in daily:
                if row["variant"] != variant or row["scenario"] != scenario:
                    continue
                unresolved |= row["net_pnl_twd"] is None
                if unresolved:
                    row["equity_twd"] = row["drawdown_twd"] = None
                else:
                    equity += float(row["net_pnl_twd"])
                    peak = max(peak, equity)
                    row["equity_twd"] = equity
                    row["drawdown_twd"] = peak - equity
    for variant in VARIANTS:
        blocked = [row for row in ledger if row["variant"] == variant and row["base_pass"]
                   and row["gate_pass"] is False]
        for row in metrics:
            if row["variant"] == variant:
                row["rejected_positive_30m"] = sum(float(x["fixed_30m_net_pnl_twd"]) > 0
                                                    for x in blocked if x["fixed_30m_net_pnl_twd"] is not None)
                row["rejected_negative_30m"] = sum(float(x["fixed_30m_net_pnl_twd"]) < 0
                                                    for x in blocked if x["fixed_30m_net_pnl_twd"] is not None)
                row["data_unknown_rejections"] = sum(x["gate_reason"] == "DATA_UNKNOWN" for x in blocked)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("coverage.csv", coverage), ("candidate_ledger.csv", ledger),
                       ("trades.csv", trades), ("daily_results.csv", daily),
                       ("experiments.csv", metrics), ("fair_comparison.csv", fair),
                       ("selection_changes.csv", changes)):
        _write_csv(out / name, rows)
    official_metadata = sorted((out / "official_reconstruction").glob("*/*/metadata.json"))
    manifest = {"analysis_id": "CHART_CHIP_SELECTION_V1", "source_head": "964edbb",
                "spec_sha256": sha256_file(SPEC), "code_sha256": sha256_file(Path(__file__)),
                "official_metadata_sha256": {str(path.relative_to(out)): sha256_file(path)
                                             for path in official_metadata},
                "source_signals_sha256": sha256_file(EARLY_DIR / "signals.csv"),
                "active_inputs_sha256": sha256_file(ACTIVE), "dates": dates,
                "official_retrieval": "POST_SIGNAL_HISTORICAL_RECONSTRUCTION",
                "reproduce": "PYTHONPATH=stock-strategy python3 -m dynamic_market_scanner_shadow_v01.chart_chip_selection run",
                "actual_orders": 0, "actual_fills": 0, "broker_order_calls": 0}
    manifest["manifest_hash"] = hashlib.sha256(canonical_bytes(manifest)).hexdigest()
    (out / "run_manifest.json").write_bytes(canonical_bytes(manifest) + b"\n")
    (out / "REPORT.md").write_text(_report(metrics, fair, coverage, changes), encoding="utf-8")
    return metrics


def _report(metrics, fair, coverage, changes):
    base = next(row for row in metrics if row["variant"] == "BASE" and row["scenario"] == "BASE")
    lines = ["# 線型＋籌碼選股探索回測", "", "已知18日回放；非樣本外、非實盤。使用官方歷史籌碼事後重建並延後一交易日，不能證明當時已捕獲同一資料版本。", "", "| 版本 | BASE 成交 | BASE 成本後損益 | 相對基準 | STRESS 已評分／全部 | STRESS 完整損益 | 擋掉30分正/負訊號 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for variant in VARIANTS:
        a = next(row for row in metrics if row["variant"] == variant and row["scenario"] == "BASE")
        b = next(row for row in metrics if row["variant"] == variant and row["scenario"] == "STRESS")
        stress = "NA" if b["account_pnl_twd"] is None else str(b["account_pnl_twd"])
        lines.append(f"| {variant} | {a['trades']} | {a['account_pnl_twd']} | {a['account_pnl_twd'] - base['account_pnl_twd']} | {b['scored']}/{b['trades']} | {stress} | {a['rejected_positive_30m']}/{a['rejected_negative_30m']} |")
    fair_base = next(row for row in fair if row["variant"] == "BASE" and row["scenario"] == "BASE")
    lines += ["", "## 共同已知日期比較", "", "無訊號但資料可用的日期仍保留；不完整資料日只在此共同範圍排除。10/23 颱風與 10/26 補假休市，10/27 的正確前日為 10/22。", "", "| 版本 | 日期 | 成交 | 成本後損益 | 相對共同基準 |", "|---|---:|---:|---:|---:|"]
    for variant in VARIANTS:
        a = next(row for row in fair if row["variant"] == variant and row["scenario"] == "BASE")
        lines.append(f"| {variant} | {a['dates']} | {a['trades']} | {a['account_pnl_twd']} | {a['account_pnl_twd'] - fair_base['account_pnl_twd']} |")
    lines += ["", "逐日改選與空手的增量分解見 selection_changes.csv；不能只將擋下的虧損相加。",
              "STRESS 若有未評分委託，完整帳戶損益為 NA，不能用已評分部分宣稱獲利。",
              "", f"BASE 與空手差 {base['account_pnl_twd']} 元。所有版本共用同一候選池、帳戶與出場。",
              "", "未知資料不是條件失敗。逐日覆蓋見 coverage.csv；逐訊號選股理由見 candidate_ledger.csv；逐筆帳戶交易見 trades.csv。",
              "日線為既有乾淨封存，部分週檔於訊號後產生；只用早於訊號日的列，但資料版本屬事後重建。籌碼在研究日後下載，僅有保守一日滯後，沒有逐次原始發布時間證據。",
              "成交、委託排隊、券商費率及當下實際可成交性仍沿用原研究假設。獨立樣本極少，不得依本結果啟用或調整正式交易。",
              "", f"共同日期: {', '.join(row['session_date'] for row in coverage)}。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "run"
    if action == "fetch":
        print(json.dumps(fetch_official(), ensure_ascii=False))
    elif action == "run":
        print(json.dumps(run(), ensure_ascii=False, indent=2))
    else:
        raise SystemExit("usage: chart_chip_selection.py [fetch|run]")
