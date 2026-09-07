from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Iterable, Iterator
import uuid

from .config import CFG, Config


SIGNAL_VALUE_FIELDS = (
    "schema_version",
    "signal_key",
    "stock_id",
    "stock_name",
    "signal_date",
    "setup",
    "parent_setup",
    "signal_close",
    "first_pivot_date",
    "first_pivot_price",
    "first_pivot_close",
    "second_pivot_date",
    "second_pivot_price",
    "second_pivot_close",
    "pivot_separation_sessions",
    "bottom_difference",
    "intervening_bounce",
    "confirmation_rebound",
    "confirmation_break_vs_prior_high",
    "confirmation_close_location",
    "first_pivot_drawdown_20",
    "signal_return_1",
    "signal_tr_vs_prior5",
    "post_vs_pre_pivot_range",
    "close_vs_sma20",
    "pivot_drawdown_20",
    "pivot_return_5",
    "pivot_atr5_vs_atr20",
    "signal_lower_wick_fraction",
    "rs_5_vs_0050",
    "pivot_volume_ratio_20",
    "signal_volume_ratio_20",
    "post_pivot_volume_vs_pivot",
    "average_volume_20",
    "average_turnover_proxy_20",
    "signal_calendar_index",
    "signal_segment_id",
    "raw_overlap",
    "compact_rule",
    "provider_name",
    "input_manifest_hash",
    "prospective_config_hash",
    "multi_setup_config_hash",
    "reversal_config_hash",
    "reversal_study_sha256",
    "compact_detector_sha256",
    "execution_mode",
    "is_actual_order",
    "is_actual_fill",
)
SIGNAL_FIELDS = SIGNAL_VALUE_FIELDS + (
    "captured_at_utc",
    "captured_at_taipei",
    "signal_payload_hash",
    "previous_record_hash",
    "record_hash",
)

SCAN_VALUE_FIELDS = (
    "schema_version",
    "signal_date",
    "scan_status",
    "provider_name",
    "input_manifest_hash",
    "prospective_config_hash",
    "compact_rule",
    "raw_n_retest_count",
    "accepted_n_retest_count",
    "compact_count",
    "stocks_with_target_bar",
    "signal_set_hash",
    "execution_mode",
    "actual_orders",
    "actual_fills",
)
SCAN_FIELDS = SCAN_VALUE_FIELDS + (
    "captured_at_utc",
    "captured_at_taipei",
    "scan_payload_hash",
    "previous_record_hash",
    "record_hash",
)

OUTCOME_VALUE_FIELDS = (
    "schema_version",
    "signal_key",
    "signal_payload_hash",
    "stock_id",
    "signal_date",
    "setup",
    "observed_through_date",
    "available_forward_sessions",
    "entry_date",
    "entry_open_proxy",
    "entry_gap",
    "day1_close_return",
    "day3_close_return",
    "day5_close_return",
    "day10_close_return",
    "mfe_close_5",
    "mfe_close_10",
    "mae_close_5",
    "mae_close_10",
    "first_target_day",
    "first_stop_day",
    "plus8_before_minus5",
    "primary_success",
    "path_result",
    "barrier_status",
    "outcome_status",
    "outcome_reason",
    "forward_window_complete",
    "is_final",
    "execution_mode",
    "is_actual_order",
    "is_actual_fill",
)
OUTCOME_FIELDS = OUTCOME_VALUE_FIELDS + (
    "revision",
    "captured_at_utc",
    "captured_at_taipei",
    "outcome_state_hash",
    "previous_record_hash",
    "record_hash",
)

OUTCOME_MONOTONIC_FIELDS = tuple(
    field
    for field in OUTCOME_VALUE_FIELDS
    if field
    not in {
        "observed_through_date",
        "available_forward_sessions",
        "barrier_status",
        "outcome_status",
        "outcome_reason",
        "forward_window_complete",
        "is_final",
    }
)


class LedgerIntegrityError(RuntimeError):
    pass


class ImmutableSignalConflict(LedgerIntegrityError):
    pass


class OutcomeRevisionConflict(LedgerIntegrityError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite values are forbidden in shadow ledgers")
        return format(value, ".17g")
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    return str(value)


def _canonical_row(values: dict, fields: tuple[str, ...]) -> dict[str, str]:
    missing = set(fields) - set(values)
    extra = set(values) - set(fields)
    if missing or extra:
        raise LedgerIntegrityError(
            f"schema mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )
    return {field: _canonical(values[field]) for field in fields}


def _payload_hash(row: dict[str, str], fields: tuple[str, ...]) -> str:
    encoded = json.dumps(
        [[field, row[field]] for field in fields],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_hash(row: dict[str, str], fields: tuple[str, ...]) -> str:
    return _payload_hash(row, tuple(field for field in fields if field != "record_hash"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _csv_lines(fields: tuple[str, ...], rows: Iterable[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _append_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    original = path.read_bytes()
    if original and not original.endswith(b"\n"):
        raise LedgerIntegrityError(f"{path.name} lacks a final newline")
    _atomic_bytes(path, original + _csv_lines(fields, rows))


def _initialize_csv(path: Path, fields: tuple[str, ...]) -> None:
    if not path.exists():
        header = _csv_lines(fields, [])
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerow(fields)
        header = output.getvalue().encode("utf-8")
        _atomic_bytes(path, header)


def _read_csv(path: Path, fields: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != fields:
            raise LedgerIntegrityError(f"{path.name} header does not match schema")
        rows = []
        for number, row in enumerate(reader, start=2):
            if set(row) != set(fields) or any(value is None for value in row.values()):
                raise LedgerIntegrityError(f"{path.name}:{number} malformed CSV row")
            rows.append(dict(row))
    previous = ""
    for number, row in enumerate(rows, start=2):
        if row["previous_record_hash"] != previous:
            raise LedgerIntegrityError(
                f"{path.name}:{number} broken previous_record_hash"
            )
        expected = _record_hash(row, fields)
        if row["record_hash"] != expected:
            raise LedgerIntegrityError(f"{path.name}:{number} record hash mismatch")
        previous = row["record_hash"]
    return rows


def _with_chain(
    values: dict,
    fields: tuple[str, ...],
    previous_record_hash: str,
) -> dict[str, str]:
    row = _canonical_row(
        values, tuple(field for field in fields if field != "record_hash")
    )
    row["record_hash"] = ""
    row["record_hash"] = _record_hash(row, fields)
    if row["previous_record_hash"] != previous_record_hash:
        raise AssertionError("caller supplied the wrong previous record hash")
    return row


def _timestamps(now: datetime) -> tuple[str, str]:
    aware = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    # Fixed offset is deliberate for durable text; Taiwan has no DST.
    taipei = utc.astimezone(__import__("zoneinfo").ZoneInfo("Asia/Taipei"))
    return utc.isoformat(), taipei.isoformat()


class ShadowStore:
    """Locked append-only CSV ledgers plus a reconstructible status cache."""

    def __init__(self, root: Path, cfg: Config = CFG) -> None:
        self.root = Path(root)
        self.cfg = cfg
        self.signals_path = self.root / cfg.signals_filename
        self.outcomes_path = self.root / cfg.outcomes_filename
        self.scans_path = self.root / cfg.scans_filename
        self.status_path = self.root / cfg.status_filename
        self.lock_path = self.root / cfg.lock_filename

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def initialize(self) -> None:
        with self.locked():
            self._initialize_unlocked()
            self._write_status_unlocked()

    def _initialize_unlocked(self) -> None:
        _initialize_csv(self.signals_path, SIGNAL_FIELDS)
        _initialize_csv(self.outcomes_path, OUTCOME_FIELDS)
        _initialize_csv(self.scans_path, SCAN_FIELDS)

    def read_signals(self) -> list[dict[str, str]]:
        with self.locked():
            self._initialize_unlocked()
            return _read_csv(self.signals_path, SIGNAL_FIELDS)

    def read_outcomes(self) -> list[dict[str, str]]:
        with self.locked():
            self._initialize_unlocked()
            return _read_csv(self.outcomes_path, OUTCOME_FIELDS)

    def read_scans(self) -> list[dict[str, str]]:
        with self.locked():
            self._initialize_unlocked()
            return _read_csv(self.scans_path, SCAN_FIELDS)

    def append_scan(
        self,
        scan_result,
        *,
        provider_name: str,
        input_manifest_hash: str,
        now: datetime | None = None,
    ) -> dict:
        """Seal one date. Exact reruns are no-ops; any drift fails closed."""

        moment = now or utc_now()
        captured_utc, captured_taipei = _timestamps(moment)
        target = scan_result.signal_date
        if target < self.cfg.prospective_start_date:
            raise LedgerIntegrityError("signals before the prospective start are forbidden")
        if scan_result.compact_count != len(scan_result.signals):
            raise LedgerIntegrityError("compact_count does not match the signal set")
        if not (
            scan_result.raw_n_retest_count
            >= scan_result.accepted_n_retest_count
            >= scan_result.compact_count
            >= 0
        ):
            raise LedgerIntegrityError("scan counts are inconsistent")
        if scan_result.stocks_with_target_bar <= 0:
            raise LedgerIntegrityError("a complete scan requires target-session stock rows")

        prepared: list[tuple[dict[str, str], str]] = []
        prepared_keys: set[tuple[str, str, str]] = set()
        for source in scan_result.signals:
            value_row = _canonical_row(dict(source), SIGNAL_VALUE_FIELDS)
            key = (
                value_row["stock_id"],
                value_row["signal_date"],
                value_row["setup"],
            )
            if (
                value_row["signal_date"] != target
                or value_row["setup"] != self.cfg.setup
            ):
                raise LedgerIntegrityError("signal row does not belong to this scan seal")
            if key in prepared_keys:
                raise LedgerIntegrityError(f"duplicate signal in scan batch: {key}")
            prepared_keys.add(key)
            semantic_hash = _payload_hash(value_row, SIGNAL_VALUE_FIELDS)
            prepared.append((value_row, semantic_hash))
        prepared.sort(key=lambda item: item[0]["signal_key"])
        set_hash = hashlib.sha256(
            "\n".join(item[1] for item in prepared).encode("ascii")
        ).hexdigest()

        with self.locked():
            self._initialize_unlocked()
            signals = _read_csv(self.signals_path, SIGNAL_FIELDS)
            scans = _read_csv(self.scans_path, SCAN_FIELDS)
            existing_signals: dict[tuple[str, str, str], dict[str, str]] = {}
            for row in signals:
                key = (row["stock_id"], row["signal_date"], row["setup"])
                if key in existing_signals:
                    raise LedgerIntegrityError(f"duplicate signal key: {key}")
                existing_signals[key] = row
            existing_scans: dict[str, dict[str, str]] = {}
            for row in scans:
                if row["signal_date"] in existing_scans:
                    raise LedgerIntegrityError(
                        f"duplicate sealed scan date: {row['signal_date']}"
                    )
                existing_scans[row["signal_date"]] = row

            logical_scan = {
                "schema_version": self.cfg.schema_version,
                "signal_date": target,
                "scan_status": "COMPLETE",
                "provider_name": provider_name,
                "input_manifest_hash": input_manifest_hash,
                "prospective_config_hash": self.cfg.fingerprint(),
                "compact_rule": self.cfg.compact_rule,
                "raw_n_retest_count": scan_result.raw_n_retest_count,
                "accepted_n_retest_count": scan_result.accepted_n_retest_count,
                "compact_count": scan_result.compact_count,
                "stocks_with_target_bar": scan_result.stocks_with_target_bar,
                "signal_set_hash": set_hash,
                "execution_mode": self.cfg.execution_mode,
                "actual_orders": 0,
                "actual_fills": 0,
            }
            canonical_scan = _canonical_row(logical_scan, SCAN_VALUE_FIELDS)
            scan_payload_hash = _payload_hash(canonical_scan, SCAN_VALUE_FIELDS)
            sealed = existing_scans.get(target)
            if sealed is not None:
                if sealed["scan_payload_hash"] != scan_payload_hash:
                    raise ImmutableSignalConflict(
                        f"sealed scan {target} differs from the original snapshot"
                    )
                stored_target = {
                    (row["stock_id"], row["signal_date"], row["setup"]): row
                    for row in signals
                    if row["signal_date"] == target
                }
                if set(stored_target) != prepared_keys:
                    raise LedgerIntegrityError(
                        f"sealed scan {target} signal keys do not match its seal"
                    )
                for value_row, semantic_hash in prepared:
                    key = (
                        value_row["stock_id"],
                        value_row["signal_date"],
                        value_row["setup"],
                    )
                    stored = existing_signals.get(key)
                    if stored is None or stored["signal_payload_hash"] != semantic_hash:
                        raise LedgerIntegrityError(
                            f"sealed scan {target} is missing its immutable signal set"
                        )
                self._write_status_unlocked(signals=signals, outcomes=None, scans=scans)
                return {"status": "EXACT_RERUN_NO_OP", "appended_signals": 0}

            if scans and target < scans[-1]["signal_date"]:
                raise LedgerIntegrityError("historical signal backfill is forbidden")
            existing_target = {
                (row["stock_id"], row["signal_date"], row["setup"]): row
                for row in signals
                if row["signal_date"] == target
            }
            if set(existing_target) - prepared_keys:
                raise ImmutableSignalConflict(
                    f"unsealed signal recovery for {target} has a different signal set"
                )

            new_signal_rows: list[dict[str, str]] = []
            previous = signals[-1]["record_hash"] if signals else ""
            for value_row, semantic_hash in prepared:
                key = (
                    value_row["stock_id"],
                    value_row["signal_date"],
                    value_row["setup"],
                )
                stored = existing_signals.get(key)
                if stored is not None:
                    if stored["signal_payload_hash"] != semantic_hash:
                        raise ImmutableSignalConflict(
                            f"immutable signal conflict for {key}"
                        )
                    continue
                values = {
                    **value_row,
                    "captured_at_utc": captured_utc,
                    "captured_at_taipei": captured_taipei,
                    "signal_payload_hash": semantic_hash,
                    "previous_record_hash": previous,
                }
                row = _with_chain(values, SIGNAL_FIELDS, previous)
                previous = row["record_hash"]
                new_signal_rows.append(row)

            # Validate the complete batch before the first durable write.
            previous_scan = scans[-1]["record_hash"] if scans else ""
            scan_values = {
                **canonical_scan,
                "captured_at_utc": captured_utc,
                "captured_at_taipei": captured_taipei,
                "scan_payload_hash": scan_payload_hash,
                "previous_record_hash": previous_scan,
            }
            scan_row = _with_chain(scan_values, SCAN_FIELDS, previous_scan)
            _append_csv(self.signals_path, SIGNAL_FIELDS, new_signal_rows)
            _append_csv(self.scans_path, SCAN_FIELDS, [scan_row])
            signals.extend(new_signal_rows)
            scans.append(scan_row)
            self._write_status_unlocked(signals=signals, outcomes=None, scans=scans)
            return {
                "status": "APPENDED",
                "appended_signals": len(new_signal_rows),
                "signal_date": target,
            }

    def append_outcomes(
        self, candidates: Iterable[dict], *, now: datetime | None = None
    ) -> dict:
        moment = now or utc_now()
        captured_utc, captured_taipei = _timestamps(moment)
        sources = list(candidates)
        with self.locked():
            self._initialize_unlocked()
            signals = _read_csv(self.signals_path, SIGNAL_FIELDS)
            immutable_signal_sha256 = sha256_file(self.signals_path)
            outcomes = _read_csv(self.outcomes_path, OUTCOME_FIELDS)
            scans = _read_csv(self.scans_path, SCAN_FIELDS)
            signal_by_key = {row["signal_key"]: row for row in signals}
            latest: dict[str, dict[str, str]] = {}
            for row in outcomes:
                expected_revision = (
                    int(latest[row["signal_key"]]["revision"]) + 1
                    if row["signal_key"] in latest
                    else 1
                )
                if int(row["revision"]) != expected_revision:
                    raise LedgerIntegrityError(
                        f"non-contiguous outcome revision for {row['signal_key']}"
                    )
                latest[row["signal_key"]] = row

            pending: list[tuple[dict[str, str], str, int]] = []
            seen_batch: set[str] = set()
            for source in sources:
                value_row = _canonical_row(dict(source), OUTCOME_VALUE_FIELDS)
                key = value_row["signal_key"]
                if key in seen_batch:
                    raise OutcomeRevisionConflict(f"duplicate outcome candidate: {key}")
                seen_batch.add(key)
                signal = signal_by_key.get(key)
                if signal is None:
                    raise OutcomeRevisionConflict(f"unknown signal key: {key}")
                if value_row["signal_payload_hash"] != signal["signal_payload_hash"]:
                    raise OutcomeRevisionConflict(f"signal hash mismatch: {key}")
                state_hash = _payload_hash(value_row, OUTCOME_VALUE_FIELDS)
                old = latest.get(key)
                if old is not None:
                    if old["outcome_state_hash"] == state_hash:
                        continue
                    if int(value_row["available_forward_sessions"]) < int(
                        old["available_forward_sessions"]
                    ):
                        raise OutcomeRevisionConflict(f"outcome regressed for {key}")
                    if value_row["observed_through_date"] < old["observed_through_date"]:
                        raise OutcomeRevisionConflict(f"outcome date regressed for {key}")
                    for field in OUTCOME_MONOTONIC_FIELDS:
                        if old[field] and value_row[field] != old[field]:
                            raise OutcomeRevisionConflict(
                                f"outcome field changed for {key}: {field}"
                            )
                    if old["is_final"] == "true":
                        raise OutcomeRevisionConflict(f"final outcome changed for {key}")
                    status_rank = {
                        "PENDING": 0,
                        "PARTIAL": 1,
                        "COMPLETE": 2,
                        "CENSORED": 2,
                    }
                    if status_rank.get(value_row["outcome_status"], -1) < status_rank.get(
                        old["outcome_status"], -1
                    ):
                        raise OutcomeRevisionConflict(f"outcome status regressed for {key}")
                    if old["barrier_status"] not in {"", "UNRESOLVED"} and value_row[
                        "barrier_status"
                    ] != old["barrier_status"]:
                        raise OutcomeRevisionConflict(f"barrier result changed for {key}")
                    if old["forward_window_complete"] == "true" and value_row[
                        "forward_window_complete"
                    ] != "true":
                        raise OutcomeRevisionConflict(f"window completion regressed for {key}")
                    revision = int(old["revision"]) + 1
                else:
                    revision = 1
                pending.append((value_row, state_hash, revision))

            previous = outcomes[-1]["record_hash"] if outcomes else ""
            rows: list[dict[str, str]] = []
            for value_row, state_hash, revision in sorted(
                pending, key=lambda item: item[0]["signal_key"]
            ):
                values = {
                    **value_row,
                    "revision": revision,
                    "captured_at_utc": captured_utc,
                    "captured_at_taipei": captured_taipei,
                    "outcome_state_hash": state_hash,
                    "previous_record_hash": previous,
                }
                row = _with_chain(values, OUTCOME_FIELDS, previous)
                previous = row["record_hash"]
                rows.append(row)
            _append_csv(self.outcomes_path, OUTCOME_FIELDS, rows)
            outcomes.extend(rows)
            self._write_status_unlocked(signals=signals, outcomes=outcomes, scans=scans)
            if sha256_file(self.signals_path) != immutable_signal_sha256:
                raise RuntimeError("outcome append modified the immutable signal ledger")
            return {
                "status": "APPENDED" if rows else "NO_OP",
                "appended_outcomes": len(rows),
            }

    def validate(self) -> dict:
        with self.locked():
            self._initialize_unlocked()
            signals = _read_csv(self.signals_path, SIGNAL_FIELDS)
            outcomes = _read_csv(self.outcomes_path, OUTCOME_FIELDS)
            scans = _read_csv(self.scans_path, SCAN_FIELDS)
            self._validate_relations(signals, outcomes, scans)
            return self._status(signals, outcomes, scans)

    def _validate_relations(
        self,
        signals: list[dict[str, str]],
        outcomes: list[dict[str, str]],
        scans: list[dict[str, str]],
    ) -> None:
        signal_by_key: dict[str, dict[str, str]] = {}
        tuple_keys: set[tuple[str, str, str]] = set()
        for row in signals:
            tuple_key = (row["stock_id"], row["signal_date"], row["setup"])
            if tuple_key in tuple_keys or row["signal_key"] in signal_by_key:
                raise LedgerIntegrityError(f"duplicate signal key: {tuple_key}")
            if row["execution_mode"] != self.cfg.execution_mode:
                raise LedgerIntegrityError("signal execution mode drifted")
            if (
                row["is_actual_order"] != "false"
                or row["is_actual_fill"] != "false"
            ):
                raise LedgerIntegrityError("signal ledger claims an order or fill")
            tuple_keys.add(tuple_key)
            signal_by_key[row["signal_key"]] = row

        scan_by_date: dict[str, dict[str, str]] = {}
        prior_date = ""
        for row in scans:
            day = row["signal_date"]
            if day in scan_by_date or (prior_date and day <= prior_date):
                raise LedgerIntegrityError("scan dates must be unique and ascending")
            if row["actual_orders"] != "0" or row["actual_fills"] != "0":
                raise LedgerIntegrityError("scan ledger claims an order or fill")
            scan_by_date[day] = row
            prior_date = day

        for day, scan in scan_by_date.items():
            members = sorted(
                (row for row in signals if row["signal_date"] == day),
                key=lambda row: row["signal_key"],
            )
            digest = hashlib.sha256(
                "\n".join(row["signal_payload_hash"] for row in members).encode(
                    "ascii"
                )
            ).hexdigest()
            if int(scan["compact_count"]) != len(members):
                raise LedgerIntegrityError(f"scan count mismatch on {day}")
            if scan["signal_set_hash"] != digest:
                raise LedgerIntegrityError(f"scan signal-set hash mismatch on {day}")
        orphan_dates = sorted(
            {row["signal_date"] for row in signals} - set(scan_by_date)
        )
        if orphan_dates:
            raise LedgerIntegrityError(
                f"unsealed signal dates require recovery: {orphan_dates[0]}"
            )

        latest_revision: dict[str, int] = {}
        for row in outcomes:
            signal = signal_by_key.get(row["signal_key"])
            if signal is None:
                raise LedgerIntegrityError("outcome references an unknown signal")
            if row["signal_payload_hash"] != signal["signal_payload_hash"]:
                raise LedgerIntegrityError("outcome references the wrong signal hash")
            expected = latest_revision.get(row["signal_key"], 0) + 1
            if int(row["revision"]) != expected:
                raise LedgerIntegrityError("outcome revisions are not contiguous")
            if (
                row["is_actual_order"] != "false"
                or row["is_actual_fill"] != "false"
            ):
                raise LedgerIntegrityError("outcome ledger claims an order or fill")
            latest_revision[row["signal_key"]] = expected

    def record_run(
        self, command: str, result: dict, now: datetime | None = None
    ) -> Path:
        moment = now or utc_now()
        captured_utc, captured_taipei = _timestamps(moment)
        run_id = (
            moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + "_"
            + uuid.uuid4().hex
        )
        path = self.root / "runs" / run_id / "run_manifest.json"
        payload = {
            "schema_version": self.cfg.schema_version,
            "run_id": run_id,
            "command": command,
            "captured_at_utc": captured_utc,
            "captured_at_taipei": captured_taipei,
            "strategy_id": self.cfg.strategy_id,
            "execution_mode": self.cfg.execution_mode,
            "config": self.cfg.snapshot(),
            "config_hash": self.cfg.fingerprint(),
            "result": result,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        _atomic_bytes(path, encoded)
        return path

    def _status(
        self,
        signals: list[dict[str, str]],
        outcomes: list[dict[str, str]],
        scans: list[dict[str, str]],
    ) -> dict:
        latest_outcomes: dict[str, dict[str, str]] = {}
        for row in outcomes:
            latest_outcomes[row["signal_key"]] = row
        return {
            "schema_version": self.cfg.schema_version,
            "strategy_id": self.cfg.strategy_id,
            "execution_mode": self.cfg.execution_mode,
            "prospective_start_date": self.cfg.prospective_start_date,
            "compact_rule": self.cfg.compact_rule,
            "config_hash": self.cfg.fingerprint(),
            "expected_reversal_study_sha256": self.cfg.expected_reversal_study_hash,
            "expected_compact_detector_sha256": self.cfg.expected_compact_detector_hash,
            "signal_rows": len(signals),
            "outcome_revision_rows": len(outcomes),
            "latest_outcome_states": len(latest_outcomes),
            "completed_outcomes": sum(
                row["is_final"] == "true" for row in latest_outcomes.values()
            ),
            "sealed_scan_dates": len(scans),
            "last_successful_signal_date": scans[-1]["signal_date"] if scans else None,
            "signals_sha256": sha256_file(self.signals_path),
            "outcomes_sha256": sha256_file(self.outcomes_path),
            "scans_sha256": sha256_file(self.scans_path),
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }

    def _write_status_unlocked(
        self,
        *,
        signals: list[dict[str, str]] | None = None,
        outcomes: list[dict[str, str]] | None = None,
        scans: list[dict[str, str]] | None = None,
    ) -> None:
        if signals is None:
            signals = _read_csv(self.signals_path, SIGNAL_FIELDS)
        if outcomes is None:
            outcomes = _read_csv(self.outcomes_path, OUTCOME_FIELDS)
        if scans is None:
            scans = _read_csv(self.scans_path, SCAN_FIELDS)
        payload = self._status(signals, outcomes, scans)
        payload["status_generated_at_utc"] = utc_now().isoformat()
        encoded = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        _atomic_bytes(self.status_path, encoded)
