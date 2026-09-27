# MFE profit protection study v0.1

Research/backtest-only exit overlay. It does not import the broker adapter and
is not imported by `yuanta_live_runtime_v01`. The environment feature flag
`ENABLE_MFE_PROFIT_PROTECTION` defaults to `false`; live behavior is unchanged.

MFE is measured from the existing executable liquidation-quote proxy rather
than raw last-trade highs/lows. This is deliberately conservative: the overlay
does not credit a favorable print that the unchanged execution model could not
have exited at.

The production entry engine, signal timing, candidate universe, quantity,
existing fixed-net-TWD stop, existing trailing/loss-recovery/reversal exits,
force-flat time, fees, tax and adverse-one-tick quote proxy are reused without
parameter fitting. The current fixed TWD 5,000 initial stop is inverted through
the same fee/tax PnL model to derive a positive price-distance `1R`.

`MFE_*` fields stop at the unchanged strategy's actual baseline exit, because a
closed position cannot later activate an overlay. Separate
`counterfactual_path_MFE_*` fields report the subsequent recorded path through
the existing force-flat horizon for diagnosis only; they never affect exits.

Run:

```bash
python3 -m mfe_profit_protection_study_v01.main \
  --session 20260924=/absolute/path/to/run \
  --output-dir mfe_profit_protection_study_v01/results
```

To diagnose every stock's first affordable signal instead of stopping after
the single executable portfolio entry, add:

```bash
--trade-universe independent-first-signals
```

That cohort may contain overlapping trades. Its aggregate PnL is diagnostic
only and must not be read as one executable NT$190,000 portfolio.

Outputs include JSON, comparison/per-trade/bucket/post-exit CSVs, Markdown and
a SHA-256 manifest. The default live-parity universe requires the existing
strict full-session coverage gate. The independent diagnostic universe mirrors
the existing all-signal validator, preserves partial-session and callback-error
warnings in its output, and must not be treated as production evidence.
