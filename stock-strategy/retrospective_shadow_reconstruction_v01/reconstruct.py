from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable

from multi_setup_study_v01.config import CFG as MULTI_CFG
from multi_setup_study_v01.setup_detectors import is_compact_retest
from prospective_shadow_v01.config import CFG as PROSPECTIVE_CFG
from prospective_shadow_v01.detector import (
    assert_frozen_contract,
    scan_snapshot,
    source_hashes,
)
from prospective_shadow_v01.market_data_provider import (
    ExistingDailyDataProvider,
    normalize_date,
)

from .config import CFG, ReconstructionConfig


PROSPECTIVE_LEDGER_FILENAMES = (
    PROSPECTIVE_CFG.signals_filename,
    PROSPECTIVE_CFG.outcomes_filename,
    PROSPECTIVE_CFG.scans_filename,
    PROSPECTIVE_CFG.status_filename,
)

# This is deliberately a research export schema, not the prospective ledger
# schema.  No capture timestamps, chain fields, revisions, or ledger keys are
# generated here.
CANDIDATE_SOURCE_FIELDS = (
    "stock_id",
    "stock_name",
    "signal_date",
    "setup",
    "parent_setup",
    "signal_close",
    "first_pivot_date",
    "first_pivot_price",
    "second_pivot_date",
    "second_pivot_price",
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
CANDIDATE_FIELDS = (
    "reconstruction_label",
    "sample_classification",
    "is_prospective_sample",
    *CANDIDATE_SOURCE_FIELDS,
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _utc_timestamp(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _normalise_csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("non-finite reconstruction values are forbidden")
        return format(value, ".17g")
    return str(value)


def _candidate_csv(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CANDIDATE_FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        missing = set(CANDIDATE_SOURCE_FIELDS) - set(row)
        if missing:
            raise RuntimeError(
                f"frozen detector output is missing fields: {sorted(missing)}"
            )
        selected = {field: row[field] for field in CANDIDATE_SOURCE_FIELDS}
        writer.writerow(
            {
                "reconstruction_label": _normalise_csv_value(
                    row["reconstruction_label"]
                ),
                "sample_classification": _normalise_csv_value(
                    row["sample_classification"]
                ),
                "is_prospective_sample": _normalise_csv_value(
                    row["is_prospective_sample"]
                ),
                **{
                    field: _normalise_csv_value(value)
                    for field, value in selected.items()
                },
            }
        )
    return output.getvalue().encode("utf-8")


def _ledger_hashes(store_dir: Path) -> dict[str, str]:
    result = {
        filename: _sha256_file(store_dir / filename)
        if (store_dir / filename).is_file()
        else None
        for filename in PROSPECTIVE_LEDGER_FILENAMES
    }
    missing = [filename for filename, digest in result.items() if digest is None]
    if missing:
        raise RuntimeError(
            "cannot prove prospective-ledger isolation; missing files: "
            + ", ".join(missing)
        )
    return {filename: str(digest) for filename, digest in result.items()}


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _metadata_candidates(path: Path) -> tuple[Path, ...]:
    candidates = (
        path.with_suffix(path.suffix + ".metadata.json"),
        path.with_suffix(".metadata.json"),
    )
    return tuple(dict.fromkeys(candidates))


def _file_provenance(path: Path) -> dict:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    item: dict = {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
    }
    for candidate in _metadata_candidates(resolved):
        if not candidate.is_file():
            continue
        try:
            metadata = json.loads(candidate.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid provenance metadata: {candidate}") from exc
        item["metadata_path"] = str(candidate)
        item["metadata_sha256"] = _sha256_file(candidate)
        item["metadata"] = metadata
        break
    return item


def _implementation_hashes() -> dict[str, str]:
    module = Path(__file__).resolve().parent
    return {
        filename: _sha256_file(module / filename)
        for filename in ("config.py", "reconstruct.py", "main.py")
    }


def _assert_current_inputs_match_snapshot(snapshot, provenance: dict) -> None:
    supplied = [
        *provenance["archives"],
        provenance["trading_calendar"],
    ]
    manifest = list(snapshot.input_manifest)
    if len(supplied) != len(manifest):
        raise RuntimeError("provider manifest length differs from supplied inputs")
    for current, captured in zip(supplied, manifest):
        if Path(current["path"]).name != captured.name:
            raise RuntimeError("provider manifest input order/name mismatch")
        if current["sha256"] != captured.sha256:
            raise RuntimeError(
                f"input changed after provider load: {current['path']}"
            )


def _projection(signal: dict, label: str, classification: str) -> dict:
    geometry = {
        "pivot_separation_sessions": signal.get("pivot_separation_sessions"),
        "bottom_difference": signal.get("bottom_difference"),
    }
    if not is_compact_retest(geometry, MULTI_CFG):
        raise RuntimeError("frozen detector emitted a row outside the N Compact rule")
    if signal.get("compact_rule") != PROSPECTIVE_CFG.compact_rule:
        raise RuntimeError("frozen detector emitted a different compact rule")
    if signal.get("setup") != PROSPECTIVE_CFG.setup:
        raise RuntimeError("frozen detector emitted a different setup")
    if signal.get("parent_setup") != PROSPECTIVE_CFG.parent_setup:
        raise RuntimeError("frozen detector emitted a different parent setup")
    return {
        **signal,
        "reconstruction_label": label,
        "sample_classification": classification,
        "is_prospective_sample": False,
    }


def _write_new_run(run_dir: Path, signals_payload: bytes, manifest: dict) -> None:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.", dir=run_dir.parent))
    try:
        (temporary / "signals.csv").write_bytes(signals_payload)
        manifest_payload = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        (temporary / "manifest.json").write_bytes(manifest_payload)
        for item in temporary.iterdir():
            with item.open("rb") as handle:
                os.fsync(handle.fileno())
        os.rename(temporary, run_dir)
        directory = os.open(run_dir.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _validate_existing_run(
    run_dir: Path,
    *,
    run_id: str,
    signals_sha256: str,
    input_manifest_hash: str,
    label: str,
) -> dict:
    signals_path = run_dir / "signals.csv"
    manifest_path = run_dir / "manifest.json"
    if not signals_path.is_file() or not manifest_path.is_file():
        raise RuntimeError(f"incomplete immutable reconstruction output: {run_dir}")
    if _sha256_file(signals_path) != signals_sha256:
        raise RuntimeError(f"immutable reconstruction signal conflict: {run_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid immutable reconstruction manifest: {run_dir}") from exc
    manifest_without_hash = {
        key: value for key, value in manifest.items() if key != "manifest_payload_sha256"
    }
    if manifest.get("manifest_payload_sha256") != _sha256_bytes(
        _canonical_json(manifest_without_hash)
    ):
        raise RuntimeError(f"immutable reconstruction manifest hash mismatch: {run_dir}")
    expected = {
        "run_id": run_id,
        "reconstruction_label": label,
        "input_manifest_hash": input_manifest_hash,
        "signals_sha256": signals_sha256,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(
                f"immutable reconstruction manifest conflict for {key}: {run_dir}"
            )
    return manifest


def reconstruct_date(
    signal_date: str,
    archive_paths: list[Path],
    trading_calendar_path: Path,
    *,
    output_dir: Path | None = None,
    prospective_store_dir: Path | None = None,
    now: datetime | None = None,
    cfg: ReconstructionConfig = CFG,
    provider_factory: Callable = ExistingDailyDataProvider,
    scanner: Callable = scan_snapshot,
) -> dict:
    """Reconstruct one explicitly sanctioned missed scan without ledger writes."""

    target = normalize_date(signal_date)
    if target not in cfg.allowed_signal_dates:
        raise RuntimeError(
            "only the explicitly sanctioned 2026-09-07/08 reconstructions are allowed"
        )
    assert_frozen_contract(PROSPECTIVE_CFG)
    if not archive_paths:
        raise ValueError("at least one repaired/audited archive is required")

    destination = Path(output_dir or cfg.output_dir).resolve()
    ledger_store = Path(prospective_store_dir or cfg.prospective_store_dir).resolve()
    if destination == ledger_store or _is_within(destination, ledger_store):
        raise RuntimeError("reconstruction output must be outside the prospective store")

    before = _ledger_hashes(ledger_store)
    archives = [Path(path).resolve() for path in archive_paths]
    calendar_path = Path(trading_calendar_path).resolve()
    provider = provider_factory(
        archives,
        trading_calendar_path=calendar_path,
    )
    snapshot = provider.load_through(target)
    if snapshot.data_through_date != target:
        raise RuntimeError("reconstruction provider did not truncate exactly at T")
    result = scanner(snapshot, target, PROSPECTIVE_CFG)
    if result.signal_date != target:
        raise RuntimeError("frozen detector returned a different signal date")
    if result.compact_count != len(result.signals):
        raise RuntimeError("frozen detector compact count disagrees with its rows")

    label = (
        f"RETROSPECTIVE_RECONSTRUCTION_{target[:4]}-{target[4:6]}-{target[6:]}"
    )
    rows = [
        _projection(dict(row), label, cfg.classification) for row in result.signals
    ]
    rows.sort(key=lambda row: (row["stock_id"], row["signal_date"], row["setup"]))
    signals_payload = _candidate_csv(rows)
    signals_sha256 = _sha256_bytes(signals_payload)
    input_provenance = {
        "archives": [_file_provenance(path) for path in archives],
        "trading_calendar": _file_provenance(calendar_path),
    }
    _assert_current_inputs_match_snapshot(snapshot, input_provenance)
    after = _ledger_hashes(ledger_store)
    if before != after:
        raise RuntimeError("prospective ledger changed during retrospective reconstruction")

    detector_hashes = source_hashes()
    implementation_hashes = _implementation_hashes()
    run_identity = {
        "signal_date": target,
        "classification": cfg.classification,
        "input_manifest_hash": snapshot.input_manifest_hash,
        "prospective_config_hash": PROSPECTIVE_CFG.fingerprint(),
        **detector_hashes,
        "reconstruction_implementation_hashes": implementation_hashes,
    }
    run_id = _sha256_bytes(_canonical_json(run_identity))
    run_dir = destination / label / run_id
    if run_dir.exists():
        manifest = _validate_existing_run(
            run_dir,
            run_id=run_id,
            signals_sha256=signals_sha256,
            input_manifest_hash=snapshot.input_manifest_hash,
            label=label,
        )
        return {
            "status": "VERIFIED_EXISTING_IMMUTABLE_OUTPUT",
            "run_dir": str(run_dir),
            "manifest": str(run_dir / "manifest.json"),
            "signals": str(run_dir / "signals.csv"),
            "counts": manifest["counts"],
            "prospective_ledgers_unchanged": True,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
        }

    manifest_without_hash = {
        "schema_version": cfg.schema_version,
        "run_id": run_id,
        "reconstruction_label": label,
        "sample_classification": cfg.classification,
        "is_prospective_sample": False,
        "signal_date": target,
        "generated_at_utc": _utc_timestamp(now),
        "policy": (
            "After-the-fact reconstruction using the frozen prospective detector. "
            "This output is excluded from every prospective ledger and sample."
        ),
        "counts": {
            "raw_n_retest": result.raw_n_retest_count,
            "accepted_n_retest": result.accepted_n_retest_count,
            "n_compact_retest": result.compact_count,
            "stocks_with_target_bar": result.stocks_with_target_bar,
        },
        "provider_name": snapshot.provider_name,
        "provider_data_through_date": snapshot.data_through_date,
        "provider_audit": snapshot.audit,
        "input_manifest_hash": snapshot.input_manifest_hash,
        "provider_input_manifest": [asdict(item) for item in snapshot.input_manifest],
        "input_provenance": input_provenance,
        "reconstruction_implementation_hashes": implementation_hashes,
        "frozen_contract": {
            "parent_setup": PROSPECTIVE_CFG.parent_setup,
            "setup": PROSPECTIVE_CFG.setup,
            "compact_rule": PROSPECTIVE_CFG.compact_rule,
            "prospective_config_hash": PROSPECTIVE_CFG.fingerprint(),
            "multi_setup_config_hash": MULTI_CFG.fingerprint(),
            "expected_reversal_config_hash": (
                PROSPECTIVE_CFG.expected_reversal_config_hash
            ),
            **detector_hashes,
        },
        "signals_filename": "signals.csv",
        "signals_sha256": signals_sha256,
        "prospective_ledger_hashes_before": before,
        "prospective_ledger_hashes_after": after,
        "prospective_ledgers_unchanged": True,
        "execution_mode": "RESEARCH_ONLY_NO_BROKER",
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
    manifest = {
        **manifest_without_hash,
        "manifest_payload_sha256": _sha256_bytes(
            _canonical_json(manifest_without_hash)
        ),
    }
    _write_new_run(run_dir, signals_payload, manifest)
    final_hashes = _ledger_hashes(ledger_store)
    if final_hashes != before:
        raise RuntimeError("prospective ledger changed while writing isolated output")
    return {
        "status": "CREATED_IMMUTABLE_RETROSPECTIVE_OUTPUT",
        "run_dir": str(run_dir),
        "manifest": str(run_dir / "manifest.json"),
        "signals": str(run_dir / "signals.csv"),
        "counts": manifest["counts"],
        "prospective_ledgers_unchanged": True,
        "actual_orders": 0,
        "actual_fills": 0,
        "broker_connections": 0,
    }
