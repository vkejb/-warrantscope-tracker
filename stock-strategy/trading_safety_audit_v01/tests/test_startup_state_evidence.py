"""Audit existing startup blockers using synthetic durable state only.

Passing means the current fail-closed refusal was reproduced, not that the
deployment is repaired or the broker is flat. Never use these tests to clear
halt, delete history, or absorb unresolved strategy exposure into a baseline.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from yuanta_broker_execution_v01 import ExecutionIntent, LiveOrderStore, Side
from yuanta_live_runtime_v01.main import _capture_baseline
from yuanta_live_runtime_v01.strategy import TAIPEI


class StartupStateEvidenceTests(TestCase):
    def reproduce_baseline_refusal(self, *, filled):
        with TemporaryDirectory(prefix="startup-state-audit-") as temporary:
            runtime = Path(temporary)
            store = LiveOrderStore(runtime / "audit.sqlite")
            order, _ = store.reserve(ExecutionIntent(
                "AUDIT-PRIOR-DAY", "TEST", Side.BUY, 1000, Decimal("100"),
            ))
            prior_day = (datetime.now(TAIPEI) - timedelta(days=3)).isoformat()
            with store.connection:
                store.connection.execute(
                    "UPDATE live_orders SET created_at=? WHERE client_order_id=?",
                    (prior_day, order.client_order_id),
                )
            if filled:
                store.record_fill(order.client_order_id, fill_id="AUDIT-FILL",
                                  quantity=1000, price="100")
            session, adapter = Mock(), Mock()
            session.account = "S00000000000"
            # Even a synthetic flat broker snapshot cannot establish which
            # unresolved old local records may be corrected or ignored.
            adapter.inspect_broker_state.return_value = SimpleNamespace(
                positions={}, open_orders=[],
            )
            args = SimpleNamespace(
                runtime_dir=runtime,
                baseline=runtime / "position_baseline.json",
                for_live_start=True,
                accept_existing_positions=False,
                reconcile_timeout=1,
            )
            try:
                with patch(
                    "yuanta_live_runtime_v01.main._connect_for_control",
                    return_value=({}, session, store, adapter),
                ), patch(
                    "yuanta_live_runtime_v01.main.load_credentials",
                    side_effect=AssertionError("audit must not load credentials"),
                ):
                    expected = "local strategy positions" if filled else "local active orders"
                    with self.assertRaisesRegex(RuntimeError, expected):
                        _capture_baseline(args, "PROD")
                adapter.submit.assert_not_called()
                adapter.submit_rescue.assert_not_called()
                self.assertFalse(args.baseline.exists())
                self.assertFalse((runtime / "position_baseline.meta.json").exists())
            finally:
                store.close()

    def test_prior_day_local_fill_blocks_automatic_baseline_even_if_mock_broker_flat(self):
        self.reproduce_baseline_refusal(filled=True)

    def test_prior_day_active_order_blocks_automatic_baseline_even_if_mock_broker_flat(self):
        self.reproduce_baseline_refusal(filled=False)
