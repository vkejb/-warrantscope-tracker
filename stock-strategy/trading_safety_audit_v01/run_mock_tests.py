"""Offline test entry point; refuses accidental network, Keychain or service use.

Tests still have to mock the vendor boundary. This guard is a second boundary,
not a broker sandbox/certification, and must never be used to start a runtime.
Only this test process's environment is changed; no persisted gate is edited.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import unittest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-dir", default=".")
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    os.environ["EXECUTION_MODE"] = "DRY_RUN"
    os.environ["ENABLE_LIVE_TRADING"] = "NO"
    # The optional contract test may reflect a real assembly only in a separate
    # reviewed read-only run, never as part of this mock-only entry point.
    os.environ.pop("YUANTA_VENDOR_DIR", None)
    violations = []

    def guard(event, parameters):
        reason = None
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo"}:
            reason = "NETWORK_FORBIDDEN"
        elif event == "subprocess.Popen":
            executable, command = parameters[:2]
            words = shlex.split(command) if isinstance(command, str) else list(command or [])
            text = " ".join(str(word) for word in words)
            names = {Path(str(executable)).name} | {Path(str(word)).name for word in words}
            if names & {"security", "caffeinate", "launchctl", "osascript"}:
                reason = "KEYCHAIN_SERVICE_OR_NOTIFICATION_PROCESS_FORBIDDEN"
            elif "yuanta_live_runtime_v01" in text and "--live" in words:
                reason = "LIVE_CHILD_PROCESS_FORBIDDEN"
        elif event == "ctypes.dlopen" and any(
            word in str(parameters[0]).lower() for word in ("yuanta", "sparkapi")
        ):
            reason = "VENDOR_LIBRARY_FORBIDDEN"
        if reason:
            # Never persist a URL, command, credential, account or payload.
            violations.append(reason)
            raise RuntimeError("Mock-only safety guard: " + reason)

    sys.addaudithook(guard)
    suite = unittest.defaultTestLoader.discover(args.start_dir, pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    print(json.dumps({
        "scope": "MOCK_ONLY_NOT_LIVE_CERTIFICATION",
        "tests_run": result.testsRun,
        "passed": result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped),
        "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped),
        "forbidden_boundary_attempts": len(violations),
        "forbidden_boundary_categories": sorted(set(violations)),
    }, sort_keys=True))
    return 0 if result.wasSuccessful() and not violations else 1


if __name__ == "__main__":
    raise SystemExit(main())
