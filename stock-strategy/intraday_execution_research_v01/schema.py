from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import hashlib
import json
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

from .config import CFG


BOOK_LEVELS = range(1, 6)


@dataclass(frozen=True, slots=True)
class Tick:
    schema_version: int
    trading_date: str
    event_timestamp_exchange: str
    event_timestamp_local_received: str
    processing_timestamp: str
    stock_code: str
    market: str
    last_price: float
    last_size: int
    cumulative_volume: int
    bid_price_1: float | None = None
    bid_size_1: int | None = None
    ask_price_1: float | None = None
    ask_size_1: int | None = None
    bid_price_2: float | None = None
    bid_size_2: int | None = None
    ask_price_2: float | None = None
    ask_size_2: int | None = None
    bid_price_3: float | None = None
    bid_size_3: int | None = None
    ask_price_3: float | None = None
    ask_size_3: int | None = None
    bid_price_4: float | None = None
    bid_size_4: int | None = None
    ask_price_4: float | None = None
    ask_size_4: int | None = None
    bid_price_5: float | None = None
    bid_size_5: int | None = None
    ask_price_5: float | None = None
    ask_size_5: int | None = None
    sequence_id: str | None = None
    source: str = "MOCK"
    is_mock: bool = True

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return parsed

    def validate(self) -> None:
        if self.schema_version != CFG.schema_version:
            raise ValueError("tick schema version mismatch")
        if not self.stock_code or not self.stock_code.isdigit():
            raise ValueError("stock_code must remain a digit string")
        if self.market not in {"TWSE", "TPEX"}:
            raise ValueError("unsupported market")
        exchange = self._time(self.event_timestamp_exchange)
        self._time(self.event_timestamp_local_received)
        self._time(self.processing_timestamp)
        if exchange.astimezone(ZoneInfo(CFG.timezone)).date().isoformat() != self.trading_date:
            raise ValueError("trading_date differs from Asia/Taipei exchange date")
        if self.last_price <= 0 or self.last_size < 0 or self.cumulative_volume < 0:
            raise ValueError("invalid trade values")
        for level in BOOK_LEVELS:
            bid = getattr(self, f"bid_price_{level}")
            ask = getattr(self, f"ask_price_{level}")
            bid_size = getattr(self, f"bid_size_{level}")
            ask_size = getattr(self, f"ask_size_{level}")
            if bid is not None and bid <= 0 or ask is not None and ask <= 0:
                raise ValueError("invalid book price")
            if bid_size is not None and bid_size < 0 or ask_size is not None and ask_size < 0:
                raise ValueError("invalid book size")

    def payload(self) -> dict:
        self.validate()
        return asdict(self)

    def canonical_json(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def duplicate_key(self) -> str:
        if self.sequence_id is not None:
            raw = f"{self.stock_code}|{self.event_timestamp_exchange}|{self.sequence_id}"
        else:
            raw = self.canonical_json()
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict) -> "Tick":
        allowed = {field.name for field in fields(cls)}
        if set(payload) != allowed:
            raise ValueError("tick payload fields differ from frozen schema")
        tick = cls(**payload)
        tick.validate()
        return tick


class QuoteAdapter(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def subscribe(self, symbols: Iterable[str]) -> None: ...
    def events(self) -> Iterable[Tick]: ...
    def healthcheck(self) -> dict: ...


TICK_FIELDS = tuple(field.name for field in fields(Tick))
