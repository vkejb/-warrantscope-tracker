# Background paper shadow v0.1

This module is invoked automatically after the existing Yuanta read-only
collector seals a `FULL_SESSION`. It replays the archived stream causally through
the current production `LiveDirectionEngine` with long-only entry, permanent
0050 market context, the current NT$3,500 disaster stop, MFE_V1 profit
protection, reversal exit, and force-flat behavior.

It never connects to a broker and never imports or calls an order adapter. The
paper fill model remains the existing immediate full-fill adverse-one-tick
proxy. Runtime outputs are immutable and gitignored under:

```text
paper_shadow_v01/runtime/days/YYYYMMDD/<paper_run_id>/
```

Each day preserves the paper trade, all entry-decision diagnostics, 5/10/15
minute EARLY_FAILURE observations, all 75 candidate impacts, a daily summary,
and SHA-256 manifest. EARLY_FAILURE remains observe-only and cannot close the
paper position.

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
