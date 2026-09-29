# Signal quality diagnostics v0.1

Read-only comparison of the existing intraday signal score, absolute volume
delta, and absolute large-trade delta for winners and losers. It reuses the
same independent first-signal universe and current outcome policy as
`trade_path_diagnostics_v01`.

The composite score is an intensity score, not a calibrated win probability.
This module does not modify stock selection, entry, exit, sizing, broker code,
or live behavior.

Example:

```bash
PYTHONPATH=stock-strategy python3 -m signal_quality_diagnostics_v01.main \
  --session 20260922=RUN_A,RUN_B \
  --session 20260923=RUN_C,RUN_D \
  --session 20260924=RUN_E \
  --output-dir stock-strategy/signal_quality_diagnostics_v01/results
```
