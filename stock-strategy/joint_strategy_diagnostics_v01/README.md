# Joint strategy diagnostics v0.1

Controlled backtest comparison of the current baseline, the side-aware
anti-chase filter, a causal 60-second continuation confirmation with a newly
calculated delayed fill, and the combined early-failure/cost-aware MFE exit.

This module is research-only and contains no broker integration or live toggle.

`three_minute_profit_floor_study.py` separately isolates a fixed 180-second
continuation gate and a cost-aware net-zero floor after the existing MFE_V1
protection arms.  It uses the same historical signals, execution prices, fees,
tax, slippage and position sizing as the baseline, and writes only research
artifacts.
