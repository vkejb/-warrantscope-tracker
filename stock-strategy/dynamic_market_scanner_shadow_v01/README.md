# Dynamic market scanner shadow v01

This module turns the existing expanded-universe production-engine diagnostics
into an auditable candidate lifecycle:

`MONITORING -> OBSERVING -> READY -> PAPER_SELECTED`, or
`REJECTED / EXPIRED`.

It is post-session causal shadow analysis. It does not import an order adapter,
open a broker connection, or submit an order. The existing expanded collector
continues to cap the premarket universe at 400 symbols and requires the prior
close to be no higher than TWD 190. This module adds an entry-time TWD 190 cap
and fee-aware whole-lot affordability check.

Run from `stock-strategy`:

```bash
python3 -m dynamic_market_scanner_shadow_v01.main --session-date YYYYMMDD
python3 -m dynamic_market_scanner_shadow_v01.aggregate
python3 -m unittest discover -s dynamic_market_scanner_shadow_v01/tests -v
```

The scheduled shadow collector publishes this diagnostic after the immutable
expanded paper replay succeeds. Failures are isolated from Top30 collection and
formal order handling.

## Frozen strict replay

`STRICT_MARKET_WIDE_V1_SPEC.md` and `strict_main.py` implement the first strict
expanded-universe replay: non-bearish market context, 60-second follow-through,
executable-bid after-cost progress, causal VWAP/breakout hold, and ranking among
candidates that mature at the same checkpoint.

```bash
python3 -m dynamic_market_scanner_shadow_v01.strict_main
```

The generated `strict_results/` evidence stays local. This research path is not
wired into the scheduler or the formal order system.
