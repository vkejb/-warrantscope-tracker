"""SHORT mock safety wrapper over the existing crash-safe paper engine.

Only a newly owned paper SQLite is permitted by the command-line layer. This
class accepts MOCK_ONLY snapshots, never reads real inventory/baseline or sends
orders. Normal LONG execution and the existing paper engine are unchanged.
"""
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from intraday_paper_execution_v01.engine import (
    PaperExecutionEngine, OrderStatus, ReconciliationError, InvalidTransition,
)


class ShortCoverSafety:
    def __init__(self, path: Path, *, account: str, symbol: str, baseline: int = 0):
        if path.suffix != '.sqlite' or not path.name.startswith('mock_short_'):
            raise ValueError('DEDICATED_MOCK_SHORT_DATABASE_REQUIRED')
        if not isinstance(baseline, int) or baseline < 0:
            raise ValueError('SYNTHETIC_BASELINE_MUST_BE_NONNEGATIVE_INTEGER')
        self.engine = PaperExecutionEngine(path)
        self.account, self.symbol, self.baseline = account, symbol, baseline
        self.needs_reconcile = True  # Also mandatory after every restart.
        with self.engine.connection:
            self.engine.connection.execute('CREATE TABLE IF NOT EXISTS short_mock_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            frozen = {'mode': 'MOCK_ONLY', 'account': account, 'symbol': symbol, 'baseline': baseline}
            old = self._get('identity')
            if old is not None and old != frozen:
                raise ValueError('MOCK_ACCOUNT_OR_BASELINE_MISMATCH')
            self._put('identity', frozen)
        self.engine.set_execution_mode('PAPER_ONLY', reason='ISOLATED_SHORT_MOCK_ONLY')

    def _get(self, key):
        row = self.engine.connection.execute('SELECT value FROM short_mock_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def _put(self, key, value):
        self.engine.connection.execute('INSERT OR REPLACE INTO short_mock_meta VALUES (?,?)', (key, json.dumps(value, sort_keys=True)))

    def alert(self, code):
        with self.engine.connection:
            self.engine._event('SHORT_MOCK_CRITICAL', None, {'severity': 'CRITICAL', 'code': code, 'account': self.account})

    def remaining(self):
        position = self.engine.position_quantities().get(self.symbol, 0)
        if position > 0:
            self.alert('BASELINE_OVERBUY_DETECTED')
            raise ReconciliationError('paper cover exceeds owned short')
        return -position

    def entry(self, key, quantity, price, at):
        if at.tzinfo is None:
            raise ValueError('TIMEZONE_REQUIRED')
        at = at.astimezone(ZoneInfo('Asia/Taipei'))
        if at.hour > 13 or (at.hour == 13 and at.minute >= 20):
            raise InvalidTransition('NO_ENTRY_AFTER_FROZEN_HARD_EXIT')
        if quantity <= 0 or quantity % 1000:
            raise ValueError('WHOLE_LOT_ENTRY_REQUIRED')
        existing = self.engine.get_by_key(key)
        if existing:
            return self.engine.submit_order(idempotency_key=key, symbol=self.symbol, side='SELL', quantity=quantity, limit_price=price)
        if any(o.intent == 'ENTRY' for o in self.engine.orders()):
            raise InvalidTransition('ONE_DAILY_ENTRY_ATTEMPT_ONLY')
        self.needs_reconcile = True
        return self.engine.submit_order(idempotency_key=key, symbol=self.symbol, side='SELL', quantity=quantity, limit_price=price)

    def fill(self, order_id, fill_id, quantity, price):
        old_fill = self.engine.connection.execute('SELECT fill_id FROM fills WHERE fill_id=?', (fill_id,)).fetchone()
        order = self.engine.get_order(order_id)
        if not old_fill and order.side.value == 'BUY' and quantity > self.remaining():
            self.needs_reconcile = True
            self.alert('COVER_WOULD_OVERBUY_BASELINE')
            raise ReconciliationError('mock fill cannot buy above baseline')
        if quantity <= 0 or quantity % 1000:
            raise ValueError('WHOLE_LOT_MOCK_FILL_REQUIRED')
        result = self.engine.record_fill(order_id, fill_id=fill_id, quantity=quantity, price=price)
        self.needs_reconcile = True
        return result

    def cancel(self, order_id):
        self.needs_reconcile = True
        return self.engine.request_cancel(order_id, 'MOCK_CANCEL_PENDING_NOT_SETTLED')

    def cancel_ack(self, order_id):
        # Do not release the reservation on callback alone. Reconcile the final
        # cumulative fill first; fills while CANCEL_PENDING remain acceptable.
        self.needs_reconcile = True
        with self.engine.connection:
            self._put('cancel_ack:' + order_id, True)
        self.alert('CANCEL_ACK_REQUIRES_FINAL_MOCK_RECONCILIATION')

    def failure(self, order_id, kind):
        if kind not in ('REJECTED', 'CANCELLED', 'UNKNOWN', 'CANCEL_FAILURE'):
            raise ValueError('UNSUPPORTED_MOCK_FAILURE')
        self.needs_reconcile = True
        with self.engine.connection:
            self._put('failure:' + order_id, kind)
        self.alert(kind + '_COVER_NOT_FLAT')
        order = self.engine.get_order(order_id)
        if order.status not in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.CANCEL_PENDING):
            self.engine.request_cancel(order_id, 'MOCK_FAILURE_WAIT_FOR_RECONCILIATION')

    def reconcile(self, *, mode, inventory, orders):
        if mode != 'MOCK_ONLY':
            raise ValueError('REAL_BROKER_SNAPSHOTS_FORBIDDEN')
        self.needs_reconcile = True
        local = {o.idempotency_key: o for o in self.engine.orders()}
        if set(local) != set(orders):
            self.alert('MOCK_ORDERS_INCOMPLETE')
            raise ReconciliationError('complete mock order snapshot required')
        for key, data in orders.items():
            order = self.engine.get_by_key(key)
            status, filled = data['status'], data['filled']
            if not isinstance(filled, int) or filled < order.filled_quantity or filled > order.quantity:
                self.alert('MOCK_CUMULATIVE_FILL_CONFLICT')
                raise ReconciliationError('invalid cumulative fill')
            if filled > order.filled_quantity:
                self.fill(order.order_id, f'reconcile:{order.order_id}:{filled}', filled - order.filled_quantity, data['fill_price'])
                order = self.engine.get_order(order.order_id)
            if status in ('CANCELED', 'REJECTED'):
                if not data.get('settled'):
                    self.alert('MOCK_TERMINAL_NOT_SETTLED')
                    raise ReconciliationError('terminal status without settlement is unsafe')
                if order.status not in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
                    if order.status != OrderStatus.CANCEL_PENDING:
                        self.engine.request_cancel(order.order_id, 'MOCK_TERMINAL_RECONCILIATION')
                    self.engine.confirm_cancel(order.order_id)
            elif status == 'UNKNOWN':
                self.alert('MOCK_ORDER_UNKNOWN')
                raise ReconciliationError('unknown order keeps cover reserved')
            elif OrderStatus(status).value != order.status.value:
                self.alert('MOCK_STATUS_MISMATCH')
                raise ReconciliationError('mock status mismatch')
        expected_inventory = self.baseline - self.remaining()
        if inventory != expected_inventory:
            self.alert('MOCK_INVENTORY_BASELINE_MISMATCH')
            raise ReconciliationError('inventory must equal synthetic baseline minus owned short')
        self.needs_reconcile = False

    def snapshot(self):
        remaining = self.remaining()
        open_orders = self.engine.orders(open_only=True)
        return {'mode': 'MOCK_ONLY', 'account': self.account, 'symbol': self.symbol,
                'baseline': self.baseline, 'baseline_source': 'SYNTHETIC_NOT_REAL_ACCOUNT',
                'remaining_filled_short': remaining, 'needs_reconcile': self.needs_reconcile,
                'status': 'FLAT' if remaining == 0 and not open_orders and not self.needs_reconcile else 'UNRESOLVED',
                'pending_orders': [o.to_dict() for o in open_orders],
                'actual_orders': 0, 'broker_connections': 0, 'live_send_available': False}

    def mock_snapshot(self, terminal_overrides=None):
        overrides = terminal_overrides or {}
        orders = {o.idempotency_key: {'status': overrides.get(o.idempotency_key, o.status.value),
                   'filled': o.filled_quantity, 'settled': True} for o in self.engine.orders()}
        return {'mode': 'MOCK_ONLY', 'inventory': self.baseline - self.remaining(), 'orders': orders}

    def manual_cover(self, fill_id, quantity, price):
        key = 'mock-manual:' + fill_id
        existing = self.engine.get_by_key(key)
        if not existing and quantity > self.remaining():
            self.alert('MANUAL_COVER_WOULD_TOUCH_BASELINE')
            raise ReconciliationError('manual cover exceeds owned filled short')
        order = self.engine.submit_order(idempotency_key=key, symbol=self.symbol, side='BUY', quantity=quantity, limit_price=price, intent='EXIT')
        self.fill(order.order_id, fill_id, quantity, price)
        if self.engine.orders(open_only=True):
            self.alert('MANUAL_COVER_REQUIRES_PENDING_ORDER_CANCELLATION')
        return order

    def cover(self, key, *, at, ask, ask_quantity, quote_at):
        if at.tzinfo is None or quote_at.tzinfo is None:
            raise ValueError('TIMEZONE_REQUIRED')
        at = at.astimezone(ZoneInfo('Asia/Taipei'))
        existing = self.engine.get_by_key(key)
        if existing:
            return existing
        if self.needs_reconcile:
            self.alert('FRESH_MOCK_RECONCILIATION_REQUIRED')
            return None
        if at.hour > 13 or (at.hour == 13 and at.minute >= 20):
            self.alert('HARD_EXIT_DUE_UNTIL_CONFIRMED_FLAT')
        reserved = sum(o.remaining_quantity for o in self.engine.orders(open_only=True) if o.side.value == 'BUY')
        available = self.remaining() - reserved
        if available <= 0:
            if reserved > self.remaining():
                self.alert('MANUAL_COVER_LEFT_EXCESS_PENDING_BUY')
            return None
        if not isinstance(ask_quantity, int) or ask_quantity < 1000 or ask is None or not math.isfinite(ask) or ask <= 0:
            self.alert('NO_SAFE_ASK_LIMIT_UP_OR_NO_SELL_DEPTH')
            return None
        if not 0 <= (at - quote_at).total_seconds() <= 3:
            self.alert('STALE_OR_FUTURE_ASK_NO_LAST_PRICE_ORDER')
            return None
        return self.engine.submit_order(idempotency_key=key, symbol=self.symbol, side='BUY', quantity=available,
                                        limit_price=ask, intent='EXIT')

    def close(self):
        self.engine.close()
