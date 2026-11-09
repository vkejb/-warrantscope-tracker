"""Aggregate immutable dynamic-scanner shadow manifests without retuning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .scanner import ANALYSIS_ID, DEFAULT_RUNTIME_DIR, POLICY_HASH


def _metrics(values: list[float]) -> dict:
    trades = [value for value in values if value != 0]
    gross_profit = sum(value for value in trades if value > 0)
    gross_loss = sum(value for value in trades if value < 0)
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trade_count": len(trades),
        "winning_trades": sum(value > 0 for value in trades),
        "losing_trades": sum(value < 0 for value in trades),
        "net_pnl_twd": round(sum(values), 2),
        "average_trade_twd": round(sum(trades) / len(trades), 2) if trades else None,
        "profit_factor": round(gross_profit / -gross_loss, 6) if gross_loss < 0 else None,
        "maximum_drawdown_twd": round(drawdown, 2),
    }


def aggregate(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> dict:
    rows = []
    for path in sorted(runtime_dir.glob("days/*/*/manifest.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("analysis_id") != ANALYSIS_ID:
            continue
        if manifest.get("dynamic_policy_hash") != POLICY_HASH:
            continue
        rows.append(manifest)
    rows.sort(key=lambda row: row["session_date"])
    return {
        "analysis_id": ANALYSIS_ID,
        "dynamic_policy_hash": POLICY_HASH,
        "session_count": len(rows),
        "unique_candidate_count": sum(int(row["unique_candidate_count"]) for row in rows),
        "production_anti_chase": _metrics([
            float(row["production_paper_net_pnl_twd"]) for row in rows
        ]),
        "confirmation_60s": _metrics([
            float(row["confirmation_60s_net_pnl_twd"]) for row in rows
        ]),
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_order_calls": 0,
        "status": "NOT_APPROVED_FOR_LIVE_TRADING",
        "sessions": [
            {
                "session_date": row["session_date"],
                "unique_candidate_count": row["unique_candidate_count"],
                "selected_signal_count": row["selected_signal_count"],
                "production_paper_net_pnl_twd": row["production_paper_net_pnl_twd"],
                "confirmation_60s_net_pnl_twd": row["confirmation_60s_net_pnl_twd"],
            }
            for row in rows
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = aggregate(args.runtime_dir.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
