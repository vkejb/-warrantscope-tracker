# Background paper shadow v0.1

This module is invoked automatically after the existing Yuanta read-only
collector seals a `FULL_SESSION`. It replays the archived stream causally through
two independent, long-only paper tracks:

- `PRODUCTION_ANTI_CHASE`: the production engine with the 2.0% opening-extension
  and 1.25% VWAP-extension entry limits.
- `ANTI_CHASE_PLUS_60S_CONFIRMATION`: the same gate followed by the fixed,
  paper-only 60-second flow and breakout confirmation with a newly calculated
  delayed fill.

Both tracks retain permanent 0050 market context, the current NT$3,500 disaster
stop, MFE_V1 profit protection, reversal exit, and force-flat behavior.

It never connects to a broker and never imports or calls an order adapter. The
paper fill model remains the existing immediate full-fill adverse-one-tick
proxy. Runtime outputs are immutable and gitignored under:

```text
paper_shadow_v01/runtime/days/YYYYMMDD/<paper_run_id>/
```

Each day preserves the two paper variants, normal entry-decision diagnostics,
separate 60-second confirmation diagnostics, 5/10/15 minute EARLY_FAILURE
observations for the production-policy track, a per-variant daily summary, and a
SHA-256 manifest. EARLY_FAILURE remains observe-only and cannot close a paper
position.

Manual replay is available for a sealed full session:

```bash
PYTHONPATH=stock-strategy python3 -m paper_shadow_v01.main \
  --run-dir /path/to/runtime/runs/<run_id> \
  --session-manifest /path/to/session_manifest.json
```

The existing automation checker also shows the newest verified paper day. A
direct status command is available without broker access:

```bash
PYTHONPATH=stock-strategy python3 -m paper_shadow_v01.status
```
