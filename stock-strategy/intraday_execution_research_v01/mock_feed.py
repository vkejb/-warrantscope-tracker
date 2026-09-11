from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
from typing import Iterable
from zoneinfo import ZoneInfo

from .config import CFG
from .schema import Tick


MOCK_TIMES = (
    "09:00:05", "09:04:00", "09:05:01", "09:10:00", "09:15:01", "09:20:00",
    "09:29:00", "09:30:01", "09:45:00", "10:00:01", "10:30:00", "11:00:01",
    "12:00:00", "13:00:01", "13:15:00", "13:29:00",
)


class MockQuoteAdapter:
    def __init__(self, trading_date: str, seed: int = CFG.mock_seed):
        self.trading_date = trading_date
        self.seed = seed
        self.connected = False
        self.symbols: tuple[str, ...] = ()
        self.reconnect_count = 0

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def subscribe(self, symbols: Iterable[str]) -> None:
        if not self.connected:
            raise RuntimeError("mock adapter is not connected")
        self.symbols = tuple(symbols)

    def simulate_reconnect(self) -> None:
        self.disconnect()
        self.reconnect_count += 1
        self.connect()

    def events(self) -> Iterable[Tick]:
        if not self.connected or not self.symbols:
            raise RuntimeError("mock adapter is not ready")
        tz = ZoneInfo(CFG.timezone)
        rng = random.Random(self.seed)
        output = []
        for symbol_index, symbol in enumerate(self.symbols):
            price = 30.0 + symbol_index * 1.25
            cumulative = 0
            for index, clock in enumerate(MOCK_TIMES):
                event = datetime.fromisoformat(f"{self.trading_date}T{clock}").replace(tzinfo=tz)
                direction = (rng.random() - 0.47) * 0.025
                if index in {7, 8}: direction += 0.012  # deterministic burst/recovery
                price = max(1.0, price * (1 + direction))
                size = 10 + ((symbol_index * 7 + index * 13) % 90)
                if index == 8: size *= 12
                cumulative += size
                receive_delay = 0.020 + (index % 4) * 0.011
                if index == 6 and symbol_index == 0:
                    receive_delay = -700.0  # receive-order anomaly, exchange time remains intact
                received = event.astimezone(timezone.utc) + timedelta(seconds=receive_delay)
                missing_book = index == 10 and symbol_index % 5 == 0
                spread = max(0.01, round(price * 0.0008, 2))
                output.append(Tick(
                    CFG.schema_version, self.trading_date, event.isoformat(), received.isoformat(),
                    received.isoformat(), symbol, "TWSE" if symbol_index % 2 == 0 else "TPEX",
                    round(price, 2), size, cumulative,
                    None if missing_book else round(price - spread / 2, 2),
                    None if missing_book else 100 + index,
                    None if missing_book else round(price + spread / 2, 2),
                    None if missing_book else 80 + index,
                    sequence_id=f"{symbol}-{index:04d}", source="MOCK", is_mock=True,
                ))
        output.sort(key=lambda tick: tick.event_timestamp_local_received)
        if output:
            output.insert(4, output[3])  # exact duplicate
        return output

    def healthcheck(self) -> dict:
        return {
            "adapter": "MOCK_ONLY", "connected": self.connected,
            "broker_connection": False, "real_quote_permission": "NOT_TESTED",
            "subscription_status": "MOCK_SUBSCRIBED" if self.symbols else "NOT_SUBSCRIBED",
            "first_quote_received": False, "latency": "SYNTHETIC_ONLY",
            "reconnect_count": self.reconnect_count,
        }
