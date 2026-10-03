# Trading safety audit v01

This package contains **diagnostic evidence, not production repairs**. It does
not run a broker session, enable trading, restart services, or repair account
state. The audit was performed against `74b615976c20544f6f74afe008fdf0edaa671842`
on 2026-10-03 (Asia/Taipei).

**A passing audit-evidence test means a currently unsafe behavior or a
fail-closed startup blocker was reproduced. It does not mean trading is safe.**
When the production implementation is repaired, replace the relevant evidence
assertions with regression tests of the intended safety invariant.

From the repository root, reproduce the isolated evidence suite:

```sh
EXECUTION_MODE=DRY_RUN ENABLE_LIVE_TRADING=NO PYTHONPATH=stock-strategy \
  /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m unittest discover -s stock-strategy/trading_safety_audit_v01/tests -v
```

Tests use synthetic identities, temporary SQLite/files, fake APIs, and mocked
network/credential boundaries. Some runtime tests extract one exact current
AST branch from the nested loop. Those tests establish the branch behavior
under stated inputs, **not** end-to-end deployed recovery or real exchange
acceptance.

See [report_20261003.md](report_20261003.md) for findings, deployed-state
limitations, and the conditions still needed before any live readiness claim.

Real account inventories, broker payloads, tokens, passwords, databases and
baseline contents are deliberately not copied into this package.
