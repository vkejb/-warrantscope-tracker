# Volume-flow entry study v01

Offline, research-only replay of the current LONG entry engine with three
pre-frozen volume-flow gates. The module is `SHADOW_ONLY` and
`NO_LIVE_ORDER`; it does not import the broker adapter or modify production
configuration.

Run from `stock-strategy`:

```bash
python3 -m volume_flow_entry_study_v01.main \
  --output-dir volume_flow_entry_study_v01/results
python3 -m unittest discover -s volume_flow_entry_study_v01/tests -v
```

The output directory is intentionally ignored by Git. Market archives and
research results remain local; only reproducible research code, the frozen
specification, and its configuration are suitable for the public repository.

The archived dates are known exploratory data, not untouched out-of-sample
evidence. A positive replay result is not a profitability guarantee and does
not authorize enabling a formal strategy.
