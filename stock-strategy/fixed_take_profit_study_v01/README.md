# Fixed take-profit study v0.1

Offline comparison of the sealed expanded-universe production paper entries
under the current MFE exit, two fixed net-profit exits, and one frozen early
profit-floor hybrid.  It has no broker dependency and cannot submit orders.

```bash
PYTHONPATH=stock-strategy python3 -m fixed_take_profit_study_v01.main
```

Outputs are local and ignored under `results/`: `summary.json`, `experiments.csv`,
`trades.csv`, `daily_results.csv`, `REPORT.md`, and `run_manifest.json`.
