# Trading safety audit v01

This package contains the dated audit and mock-only safety repair regressions.
It does not run a broker session, enable trading, restart services, or repair
real account state. The original audit was performed against
`74b615976c20544f6f74afe008fdf0edaa671842` on 2026-10-03 (Asia/Taipei).

The original [report_20261003.md](report_20261003.md) is preserved as historical
evidence. Repaired B/E/S/entry-risk assertions now test the intended safety
invariant; startup tests still require unresolved account state to be refused.
**Passing mock tests do not certify production trading or prove an account flat.**

From the repository root, reproduce the isolated evidence suite:

```sh
EXECUTION_MODE=DRY_RUN ENABLE_LIVE_TRADING=NO PYTHONPATH=stock-strategy \
  /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests \
  --start-dir stock-strategy/trading_safety_audit_v01/tests --verbose
```

The runner refuses real Python network calls, Keychain/service/notification
processes and LIVE subprocesses. It reports attempted forbidden boundaries even
if tested code catches the exception. Its DRY_RUN/NO values affect only the test
process; it never rewrites `.env`, Keychain, installed gates or LaunchAgents.
It is a secondary guard, not a sandbox for arbitrary native SDK code. Tests
must still explicitly replace SDK/session/API boundaries with synthetic fakes.

Run the deployed interpreter's broker/runtime tests from `stock-strategy`:

```sh
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir yuanta_broker_execution_v01/tests
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.run_mock_tests --start-dir yuanta_live_runtime_v01/tests
```

Tests use synthetic identities, temporary SQLite/files, fake APIs, and mocked
network/credential boundaries. Some runtime tests extract one exact current
AST branch from the nested loop. Those tests establish the branch behavior
under stated inputs, **not** end-to-end deployed recovery or real exchange
acceptance.

See [repair_report_20261003.md](repair_report_20261003.md) for the safety patches,
final test results, unchanged strategy boundary and remaining limitations.

Real account inventories, broker payloads, tokens, passwords, databases and
baseline contents are deliberately not copied into this package.

## Offline preparation follow-up

`offline_readiness` reads explicit local inputs without importing the trading
runtime, loading credentials, contacting a broker or modifying original files.
It is **not** a LIVE gate or an automatic state repair. Even a clean local report
returns `NOT_LIVE_CERTIFIED` and exit code **3**; invalid CLI configuration returns
2. Do not use exit code 3 as an instruction to clear HALT or retry a LIVE launch.

From `stock-strategy`, inspect the current configured paths for a target date:

```sh
/Library/Frameworks/Python.framework/Versions/3.10/bin/python3 -B \
  -m trading_safety_audit_v01.offline_readiness \
  --repo-dir .. \
  --runtime-dir /Users/linyunyan/Downloads/-warrantscope-tracker/stock-strategy/yuanta_live_runtime_v01/runtime \
  --plist /Users/linyunyan/Library/LaunchAgents/com.linyunyan.warrantscope.trading-bot.plist \
  --calendar shadow_daily_runner/runtime/trading_calendar.csv \
  --seal-dir stage_a_prospective_watchlist_v01/runtime/seals \
  --trading-date 2026-10-05
```

The date and paths must be supplied explicitly; update them for each inspection.
Future-date inspection does not create that day's inventory baseline. Nonempty
WAL/journal means store evidence is unverified. Otherwise SQLite is opened only
on a stable, private temporary copy, never on the original runtime database.
The tool suppresses accounts, holdings, quantities, credentials, private paths
and raw exceptions. Aggregate local evidence is never actual broker proof.

The package and tests now have side-effect-free `__init__.py` files, so the
repository's full unittest discovery also includes the isolated audit tests.
See [offline_preparation_20261003.md](offline_preparation_20261003.md) for the
additional account-lock fix, final results and remaining authorization boundary.
