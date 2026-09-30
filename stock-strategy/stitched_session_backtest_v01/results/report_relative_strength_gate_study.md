# Relative-strength gate controlled backtest

## Controlled daily comparison

| Session | Source quality | Baseline trade / PnL | RS disabled trade / PnL | Difference | RS rejects | Newly eligible |
|---|---|---|---|---:|---:|---:|
| 20260929 | PARTIAL_SESSION | 3605 / -3579 | 3605 / -3579 | 0 | 4 | 0 |
| 20260930 | DIAGNOSTIC_STITCH_WITH_4_UNTIMESTAMPED_CALLBACK_ERRORS | - / 0 | - / 0 | 0 | 2 | 0 |

- Baseline total: NT$-3579
- RS disabled total: NT$-3579
- Difference: NT$0
- Newly eligible after removing only RS: 0

## Rejected-path diagnostic (not causal strategy PnL)

- Raw rejected events: 6 (1 win / 5 loss), forced-entry sum NT$-8292
- 15-minute episode clusters: 4 (1 win / 3 loss), first-signal sum NT$-962
- These paths deliberately bypass confirmation and anti-chase gates and therefore do not measure the isolated RS gate effect.

## Conclusion

`NO_MEASURABLE_MARGINAL_EFFECT_IN_AVAILABLE_SAMPLE`

The available sample does not show that the RS gate changed an actual entry or realized PnL. The rejected paths lean negative, but the sample is too small and correlated to validate the gate.

## Limitations

- Only two sessions contain synchronized 0050 market context for this policy.
- Neither session is certified as a clean multi-day validation sample.
- The six rejected events include repeated signals from the same stock episode.
- No production strategy or live behavior was changed.
