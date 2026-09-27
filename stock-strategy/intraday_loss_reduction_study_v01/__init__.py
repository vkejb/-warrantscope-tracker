"""Fixed-rule diagnostic for reducing losses in the recorded intraday cohort."""

from .analysis import build_report, load_baseline_rows, write_report

__all__ = ["build_report", "load_baseline_rows", "write_report"]
