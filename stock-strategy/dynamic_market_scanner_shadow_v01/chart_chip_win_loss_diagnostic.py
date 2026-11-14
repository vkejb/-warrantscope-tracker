"""Read-only, descriptive winner/loser attribution for the frozen chart/chip study.

This module never reselects candidates, changes exits, or calls a broker/network API.
It deliberately separates a fixed 30-minute signal mark from account exit P&L.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
from statistics import median

from .chart_chip_selection import DEFAULT_OUT as SOURCE, _chips
from .strict_replay import _write_csv


OUT = Path(__file__).resolve().parent / "chart_chip_win_loss_diagnostic_results"


def _read(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def _label(value: str | None) -> str:
    number = _number(value)
    if number is None:
        return "UNKNOWN"
    return "WIN" if number > 0 else "LOSS" if number < 0 else "FLAT"


def _group(rows: list[dict], outcome: str) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["chart_pass"], row["chip_pass"])].append(row)
    result = []
    for chart, chip in (("True", "True"), ("True", "False"),
                        ("False", "True"), ("False", "False")):
        subset = grouped[(chart, chip)]
        known = [row for row in subset if row[outcome] != "UNKNOWN"]
        values = [float(row["pnl_twd"]) for row in known]
        result.append({"outcome": outcome, "chart_pass": chart, "chip_pass": chip,
                       "signals_or_trades": len(subset), "scored": len(known),
                       "wins": sum(row[outcome] == "WIN" for row in known),
                       "losses": sum(row[outcome] == "LOSS" for row in known),
                       "flat": sum(row[outcome] == "FLAT" for row in known),
                       "unknown": len(subset) - len(known),
                       "sum_pnl_twd": sum(values) if known else None,
                       "median_pnl_twd": median(values) if known else None,
                       "distinct_dates": len({row["session_date"] for row in known})})
    return result


def _render(groups: list[dict], account: list[dict], signals: list[dict]) -> str:
    lines = ["# 線型／籌碼與獲利、虧損的描述性比較", "",
             "只用前一輪凍結的 18 日資料；此批日期已被看過，並非樣本外驗證。",
             "線型=前日收盤價>5日均價>20日均價；籌碼=前日三大法人合計淨買超。",
             "籌碼是訊號後取得的官方歷史資料重建，不是當時的盤前捕獲證據。",
             "", "## 兩種結果不可混為一談", "",
             "固定30分鐘以進場時模擬成交價、30分鐘後可得買價和原成本模型計算；它不是實際策略出場，也不是單純股價漲跌。",
             "帳戶結果則是每日最多一筆、依既有出場規則完成的模擬交易。兩者不應相加。", ""]
    for outcome, title in (("fixed_30m_label", "全部基準合格訊號：固定30分鐘淨損益"),
                           ("account_label", "原規則帳戶：實際模擬出場淨損益")):
        lines.extend([f"### {title}", "",
                      "| 線型 | 籌碼 | 總數 | 已評分 | 賺／賠／持平 | 未知 | 淨損益合計 |",
                      "|---|---|---:|---:|---:|---:|---:|"])
        for row in (item for item in groups if item["outcome"] == outcome):
            pnl = "NA" if row["sum_pnl_twd"] is None else f"{row['sum_pnl_twd']:,.0f}"
            lines.append(f"| {row['chart_pass']} | {row['chip_pass']} | "
                         f"{row['signals_or_trades']} | {row['scored']} | "
                         f"{row['wins']}/{row['losses']}/{row['flat']} | "
                         f"{row['unknown']} | {pnl} |")
        lines.append("")
    lines += ["## 賺與賠的盤前特徵", "",
              "| 結果口徑 | 分組 | 筆數 | 法人淨買超 | 線型通過 | 外資淨買超 |",
              "|---|---|---:|---:|---:|---:|"]
    for label_name, population, title in (("fixed_30m_label", signals, "固定30分"),
                                          ("account_label", account, "帳戶出場")):
        for label in ("WIN", "LOSS"):
            subset = [row for row in population if row[label_name] == label]
            lines.append(f"| {title} | {label} | {len(subset)} | "
                         f"{sum(row['chip_pass'] == 'True' for row in subset)} | "
                         f"{sum(row['chart_pass'] == 'True' for row in subset)} | "
                         f"{sum(row['foreign_net_shares'] > 0 for row in subset)} |")
    lines += ["", "這些是同一小樣本內的描述，不是預測準確率或篩選門檻。"
              "法人淨股數受股本與成交量影響，跨股票直接比較絕對股數沒有可比性。", ""]
    lines += ["## 九筆帳戶交易：逐筆反例也保留", "",
              "| 日期 | 股票 | 線型 | 籌碼 | 前日法人淨股數 | 帳戶淨損益 | 30分淨損益 | 出場原因 |",
              "|---|---|---|---|---:|---:|---:|---|"]
    for row in account:
        mark = "UNKNOWN" if row["fixed_30m_pnl_twd"] is None else f"{row['fixed_30m_pnl_twd']:,.0f}"
        lines.append(f"| {row['session_date']} | {row['stock_id']} | "
                     f"{row['chart_pass']} | {row['chip_pass']} | "
                     f"{row['chip_net_shares']:,} | {row['pnl_twd']:,.0f} | "
                     f"{mark} | {row['exit_reason']} |")
    lines += ["", "## 解讀邊界", "",
              "固定30分的法人淨賣超組雖均為虧損，原策略仍有淨賣超獲利交易；不能說法人賣超必跌。",
              "6226 固定30分淨虧但原策略出場淨賺，說明觀察期限會改變標籤；6668 成本前持平但扣成本後虧損。",
              "線型通過組在本批資料中全屬法人淨買超，因此無法估計線型與籌碼各自獨立、交互的因果效果。",
              "逐筆數與獨立交易日都少；同日多股相關，不應把訊號數當獨立樣本。",
              "未比較前日線型以外的完整圖形、融資融券、主力分點或當日未公布籌碼；不得據此新增門檻或實盤自動送單。",
              "來源雜湊、法人分項、均線距離及逐訊號狀態見 CSV 與 manifest。", ""]
    return "\n".join(lines)


def run(source: Path = SOURCE, out: Path = OUT) -> dict:
    manifest = json.loads((source / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest["analysis_id"] != "CHART_CHIP_SELECTION_V1" or manifest["actual_orders"] != 0:
        raise RuntimeError("unexpected upstream research identity or order state")
    ledger_path, trades_path = source / "candidate_ledger.csv", source / "trades.csv"
    ledger = [row for row in _read(ledger_path)
              if row["variant"] == "BASE" and row["base_pass"] == "True"]
    trade_rows = [row for row in _read(trades_path)
                  if row["variant"] == "BASE" and row["scenario"] == "BASE"
                  and row["status"] != "NO_TRADE"]
    by_key = {(row["session_date"], row["stock_id"]): row for row in ledger}
    if len(by_key) != len(ledger):
        raise RuntimeError("duplicate baseline candidate identity")
    signals = []
    for row in ledger:
        symbol, day = row["stock_id"], row["prior_source_date"]
        chips, status = _chips(source, day, {symbol})
        if status != "HISTORICAL_RECONSTRUCTION" or not chips or symbol not in chips:
            raise RuntimeError(f"prior institutional data unavailable: {day}/{symbol}")
        item = chips[symbol]
        pnl = _number(row["fixed_30m_net_pnl_twd"])
        signals.append({"session_date": row["session_date"], "stock_id": symbol,
                        "signal_time": row["signal_time"], "prior_source_date": day,
                        "chart_pass": row["chart_pass"], "chip_pass": row["chip_pass"],
                        "prior_close": float(row["prior_close"]), "ma5": float(row["ma5"]),
                        "ma20": float(row["ma20"]),
                        "close_minus_ma5_pct": (float(row["prior_close"]) / float(row["ma5"]) - 1) * 100,
                        "ma5_minus_ma20_pct": (float(row["ma5"]) / float(row["ma20"]) - 1) * 100,
                        "foreign_net_shares": item["foreign"],
                        "trust_net_shares": item["investment_trust"],
                        "dealer_net_shares": item["dealer"],
                        "chip_net_shares": int(row["chip_net_shares"]),
                        "pnl_twd": pnl, "fixed_30m_label": _label(row["fixed_30m_net_pnl_twd"]),
                        "actual_orders": 0})
    signal_by_key = {(row["session_date"], row["stock_id"]): row for row in signals}
    account = []
    for trade in trade_rows:
        key = trade["session_date"], trade["stock_id"]
        source_signal = signal_by_key[key]
        value = _number(trade["net_pnl_twd"])
        account.append({**source_signal, "pnl_twd": value,
                        "account_label": _label(trade["net_pnl_twd"]),
                        "fixed_30m_pnl_twd": source_signal["pnl_twd"],
                        "entry_time": trade["entry_time"], "entry_price": trade["entry_price"],
                        "exit_time": trade["exit_time"], "exit_price": trade["exit_price"],
                        "exit_reason": trade["exit_reason"], "gross_pnl_twd": trade["gross_pnl"],
                        "commission_twd": trade["commission"], "sell_tax_twd": trade["sell_tax"]})
    groups = _group(signals, "fixed_30m_label") + _group(account, "account_label")
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "all_signals.csv", signals)
    _write_csv(out / "account_trades.csv", account)
    _write_csv(out / "comparison.csv", groups)
    (out / "REPORT.md").write_text(_render(groups, account, signals), encoding="utf-8")
    result = {"analysis_id": "CHART_CHIP_WIN_LOSS_DIAGNOSTIC_V1",
              "upstream_manifest_hash": manifest["manifest_hash"],
              "candidate_ledger_sha256": _sha(ledger_path), "trades_sha256": _sha(trades_path),
              "signals": len(signals), "account_trades": len(account),
              "actual_orders": 0, "network_calls": 0}
    (out / "manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
