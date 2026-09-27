from __future__ import annotations

import argparse
from pathlib import Path

from .analysis import build_report, load_baseline_rows, write_report


MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = MODULE_DIR.parent / "mfe_profit_protection_study_v01" / "results_independent_signals" / "per_trade_comparison.csv"
DEFAULT_OUTPUT = MODULE_DIR / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixed-rule intraday loss-reduction diagnostic")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    rows = load_baseline_rows(args.input.resolve())
    report = build_report(rows)
    write_report(report, args.output_dir.resolve())
    for row in report["summaries"]:
        print(
            f"{row['variant']}: trades={row['trades']} net={row['net_pnl']:.0f} "
            f"PF={row['profit_factor'] if row['profit_factor'] is not None else 'N/A'} "
            f"without_top={row['net_without_largest_winner']:.0f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
