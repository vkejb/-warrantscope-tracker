from __future__ import annotations
import argparse
from pathlib import Path
from .indicator_stop_analysis import build_indicator_report, write_indicator_report

MODULE_DIR = Path(__file__).resolve().parent

def _session(value: str):
    date, paths = value.split("=", 1)
    return date, [Path(path).resolve() for path in paths.split(",")]

def main(argv=None):
    parser = argparse.ArgumentParser(description="Yuanta flow indicator stop diagnostic")
    parser.add_argument("--session", action="append", required=True, type=_session)
    parser.add_argument("--capital", type=int, default=190_000)
    parser.add_argument("--output-dir", type=Path, default=MODULE_DIR / "indicator_stop_results")
    args = parser.parse_args(argv)
    report = build_indicator_report(dict(args.session), args.capital)
    write_indicator_report(report, args.output_dir.resolve())
    for row in report["summaries"]:
        print(f"{row['variant']}: net={row['net_pnl']:.0f} PF={row['profit_factor']:.3f} exits={row['indicator_exits']} recovered={row['indicator_exits_then_recovered_positive']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
