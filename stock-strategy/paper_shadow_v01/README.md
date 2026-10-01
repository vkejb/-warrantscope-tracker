# Background paper shadow v0.1

This module is invoked automatically after the existing Yuanta read-only
collector seals a `FULL_SESSION`. It replays the archived stream causally through
four independent, long-only paper tracks:

- `PRODUCTION_ANTI_CHASE`: the production engine with the 2.0% opening-extension
  and 1.25% VWAP-extension entry limits.
- `ANTI_CHASE_PLUS_60S_CONFIRMATION`: the same gate followed by the fixed,
  paper-only 60-second flow and breakout confirmation with a newly calculated
  delayed fill.
- `RECOVERY_NET_MFE_BUFFER_0_30_SHADOW`: the production anti-chase entry with
  the existing NT$3,500 hard stop, a one-time recovery-aware 120-second failure
  check, and a cost-aware net-MFE floor armed at 0.75R with 0.30R initially
  locked.
- `RECOVERY_NET_MFE_BUFFER_0_40_SHADOW`: the same diagnostic with 0.40R
  initially locked.

The two production-comparison tracks retain permanent 0050 market context, the
current NT$3,500 disaster stop, MFE_V1 profit protection, reversal exit, and
force-flat behavior. The two buffered variants reuse the exact production entry
and replace only the exit overlay in post-session paper replay. Existing
non-MFE exits remain authoritative.

It never connects to a broker and never imports or calls an order adapter. The
paper fill model remains the existing immediate full-fill adverse-one-tick
proxy. Runtime outputs are immutable and gitignored under:

```text
paper_shadow_v01/runtime/days/YYYYMMDD/<paper_run_id>/
```

Each day preserves the two paper variants, normal entry-decision diagnostics,
separate 60-second confirmation diagnostics, 5/10/15 minute EARLY_FAILURE
observations for the production-policy track, both buffered-exit outcomes, a
per-variant daily summary, and a SHA-256 manifest. EARLY_FAILURE remains
observe-only on the production tracks; the one-time 120-second recovery-aware
exit applies only to the two explicitly named buffered paper variants.

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

Every successful automatic paper publication also refreshes a verified
cross-day comparison. It excludes older contract versions, corrupted manifests,
and ambiguous duplicate dates. Until each buffered variant has at least 20
paired trades it remains `COLLECTING`; even after that threshold it only reports
a shadow evidence gate and never changes production behavior:

```bash
PYTHONPATH=stock-strategy python3 -m paper_shadow_v01.comparison
```
