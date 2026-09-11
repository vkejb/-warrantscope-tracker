from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

import numpy as np


PARSER_SCHEMA_VERSION = 2
IDENTITY_SCHEMA_VERSION = 2
SOURCE_ORDER = (
    "TWSE_INSTITUTIONAL",
    "TPEX_INSTITUTIONAL",
    "TWSE_MARGIN",
    "TPEX_MARGIN",
)
OFFICIAL_ETF_MASTER = {
    "TWSE:006203": {
        "market": "TWSE", "security_code": "006203", "security_name": "元大MSCI台灣",
        "security_type": "ETF",
        "evidence_url": "https://www.twse.com.tw/zh/ETFortune/etfInfo/006203",
    },
    "TWSE:006204": {
        "market": "TWSE", "security_code": "006204", "security_name": "永豐臺灣加權",
        "security_type": "ETF",
        "evidence_url": "https://www.twse.com.tw/zh/ETFortune/etfInfo/006204",
    },
    "TWSE:006205": {
        "market": "TWSE", "security_code": "006205", "security_name": "富邦上証",
        "security_type": "ETF",
        "evidence_url": "https://www.twse.com.tw/zh/ETFortune/etfInfo/006205",
    },
    "TWSE:006206": {
        "market": "TWSE", "security_code": "006206", "security_name": "元大上證50",
        "security_type": "ETF",
        "evidence_url": "https://www.twse.com.tw/zh/ETFortune/etfInfo/006206",
    },
    "TWSE:006207": {
        "market": "TWSE", "security_code": "006207", "security_name": "復華滬深",
        "security_type": "ETF",
        "evidence_url": "https://www.twse.com.tw/zh/ETFortune/etfInfo/006207",
    },
    "TWSE:008201": {
        "market": "TWSE", "security_code": "008201", "security_name": "BP上證50",
        "security_type": "ETF",
        "evidence_url": "https://www.twse.com.tw/rwd/staticFiles/product/publication/0005000023.pdf",
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_digest(paths: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: str(item.relative_to(root))):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_immutable(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"immutable v2 artifact collision: {path}")
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _integer(value: object) -> int:
    text = str(value).replace(",", "").strip()
    return int(text) if text not in {"", "--"} else 0


def _source_metadata(source: str) -> tuple[str, str]:
    return (
        "TWSE" if source.startswith("TWSE") else "TPEX",
        "INSTITUTIONAL" if source.endswith("INSTITUTIONAL") else "MARGIN_SHORT",
    )


def _raw_table(source: str, raw_payload: dict) -> tuple[list[str], list[list]]:
    if source == "TWSE_INSTITUTIONAL":
        if raw_payload.get("stat") != "OK":
            raise RuntimeError("TWSE institutional raw status is not OK")
        return raw_payload["fields"], raw_payload.get("data", [])
    if source == "TPEX_INSTITUTIONAL":
        if raw_payload.get("stat") != "ok":
            raise RuntimeError("TPEx institutional raw status is not ok")
        table = raw_payload.get("tables", [{}])[0]
        return table["fields"], table.get("data", [])
    if source == "TWSE_MARGIN":
        if raw_payload.get("stat") != "OK":
            raise RuntimeError("TWSE margin raw status is not OK")
        table = next(
            (item for item in raw_payload.get("tables", []) if "融資融券彙總" in item.get("title", "")),
            None,
        )
        if table is None:
            raise RuntimeError("TWSE margin detail table is absent")
        return table["fields"], table.get("data", [])
    if source == "TPEX_MARGIN":
        if raw_payload.get("stat") != "ok":
            raise RuntimeError("TPEx margin raw status is not ok")
        table = raw_payload.get("tables", [{}])[0]
        return table["fields"], table.get("data", [])
    raise ValueError(f"unknown official source: {source}")


def _feature_values(source: str, fields: list[str], row: list) -> dict[str, int]:
    if source == "TWSE_INSTITUTIONAL":
        return {
            "foreign": _integer(row[next(i for i, field in enumerate(fields) if "外陸資買賣超股數(不含外資自營商)" in field)]),
            "investment_trust": _integer(row[fields.index("投信買賣超股數")]),
            "dealer": _integer(row[fields.index("自營商買賣超股數")]),
        }
    if source == "TPEX_INSTITUTIONAL":
        return {"foreign": _integer(row[10]), "investment_trust": _integer(row[13]), "dealer": _integer(row[22])}
    if source == "TWSE_MARGIN":
        return {"margin_balance": _integer(row[6]), "short_balance": _integer(row[12])}
    if source == "TPEX_MARGIN":
        return {"margin_balance": _integer(row[6]), "short_balance": _integer(row[14])}
    raise ValueError(source)


def parse_raw_source_v2(
    source: str,
    source_date: int,
    raw: bytes,
    wanted_research_codes: set[str],
    source_url: str,
) -> tuple[dict, list[dict]]:
    """Parse one immutable official payload without coercing security identity to int."""
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid immutable raw JSON for {source} {source_date}") from exc
    payload_date = int(payload.get("date", 0))
    if payload_date != source_date:
        raise RuntimeError(f"raw source/date mismatch for {source} {source_date}: {payload_date}")
    market, family = _source_metadata(source)
    fields, source_rows = _raw_table(source, payload)
    if not source_rows:
        raise RuntimeError(f"empty official rows for {source} {source_date}")
    seen_codes: set[str] = set()
    rows: list[dict] = []
    exclusions: list[dict] = []
    for raw_row in source_rows:
        security_code = str(raw_row[0]).strip()
        security_name = str(raw_row[1]).strip()
        if security_code in seen_codes:
            raise RuntimeError(f"duplicate exact official code {market}:{security_code} on {source_date}")
        seen_codes.add(security_code)
        if security_code in wanted_research_codes:
            rows.append({
                "market": market,
                "security_code": security_code,
                "security_name": security_name,
                "security_type": "COMMON_STOCK",
                "source_date": source_date,
                **_feature_values(source, fields, raw_row),
            })
            continue
        if security_code.isdigit() and str(int(security_code)) in wanted_research_codes:
            identity = OFFICIAL_ETF_MASTER.get(f"{market}:{security_code}")
            if identity is None:
                raise RuntimeError(
                    f"unmapped numeric-suffix identity {market}:{security_code} on {source_date}"
                )
            if identity["security_type"] != "ETF" or identity["security_name"] != security_name:
                raise RuntimeError(f"official security master mismatch for {market}:{security_code}")
            exclusions.append({
                "market": market,
                "security_code": security_code,
                "security_name": security_name,
                "security_type": identity["security_type"],
                "source_date": source_date,
                "reason": "OFFICIAL_SECURITY_TYPE_NOT_IN_FROZEN_COMMON_STOCK_UNIVERSE",
                "evidence_url": identity["evidence_url"],
            })
    rows.sort(key=lambda item: (item["market"], item["security_type"], item["security_code"]))
    return {
        "schema_version": PARSER_SCHEMA_VERSION,
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "source": source,
        "market": market,
        "feature_family": family,
        "source_date": source_date,
        "source_url": source_url,
        "source_row_count": len(source_rows),
        "wanted_research_code_count": len(wanted_research_codes),
        "matched_row_count": len(rows),
        "security_type_mapping_complete_for_used_rows": True,
        "raw_sha256": _sha256_bytes(raw),
        "raw_reference": f"../official_cache/entries/{source}/{source_date}/raw.json",
        "rows": rows,
    }, exclusions


def _load_v1_metadata(v1_cache: Path, source: str, date: int, raw_sha256: str) -> dict:
    parsed_path = v1_cache / "entries" / source / str(date) / "parsed.json"
    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
    if parsed.get("raw_sha256") != raw_sha256:
        raise RuntimeError(f"v1 raw/parsed hash mismatch for {source} {date}")
    return parsed


def _git_head(repo: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True, capture_output=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def rebuild_v2_from_immutable_raw(
    *,
    package: Path,
    repo: Path,
    needed_codes_by_date: dict[int, set[int]],
) -> dict:
    runtime = package / "runtime"
    v1_cache = runtime / "official_cache"
    v2_cache = runtime / "parsed_v2"
    output = runtime / "chip_daily_store_v2.npz"
    final_manifest_path = runtime / "chip_raw_manifest_v2.json"
    if any(path.exists() for path in (v2_cache, output, final_manifest_path)):
        raise FileExistsError("refusing to overwrite existing v2 parsed/final artifacts")

    dates = sorted(needed_codes_by_date)
    expected_pairs = len(dates) * len(SOURCE_ORDER)
    raw_paths = [
        v1_cache / "entries" / source / str(date) / "raw.json"
        for date in dates for source in SOURCE_ORDER
    ]
    old_parsed_paths = [
        v1_cache / "entries" / source / str(date) / "parsed.json"
        for date in dates for source in SOURCE_ORDER
    ]
    missing_raw = [str(path) for path in raw_paths if not path.exists()]
    missing_old_parsed = [str(path) for path in old_parsed_paths if not path.exists()]
    if missing_raw or missing_old_parsed or len(raw_paths) != 5864:
        raise RuntimeError(
            f"raw coverage gate failed: pairs={len(raw_paths)} missing_raw={len(missing_raw)} "
            f"missing_old_parsed={len(missing_old_parsed)}"
        )
    raw_tree_before = tree_digest(raw_paths, v1_cache)
    old_parsed_tree_before = tree_digest(old_parsed_paths, v1_cache)
    source_manifest_path = v1_cache / "download_manifest.jsonl"
    source_manifest_sha256 = sha256_path(source_manifest_path)

    temporary = Path(tempfile.mkdtemp(prefix=".parsed_v2.", dir=runtime))
    parse_events: list[dict] = []
    exclusions: list[dict] = []
    parse_errors: list[str] = []
    parsed_by_date: dict[int, dict[str, dict]] = defaultdict(dict)
    security_observations: dict[tuple[str, str], dict] = {}
    try:
        for date in dates:
            wanted = {str(int(code)) for code in needed_codes_by_date[date]}
            for source in SOURCE_ORDER:
                raw_path = v1_cache / "entries" / source / str(date) / "raw.json"
                raw = raw_path.read_bytes()
                raw_sha = _sha256_bytes(raw)
                old = _load_v1_metadata(v1_cache, source, date, raw_sha)
                try:
                    parsed, local_exclusions = parse_raw_source_v2(
                        source, date, raw, wanted, old["source_url"]
                    )
                except Exception as exc:
                    parse_errors.append(f"{source}:{date}:{type(exc).__name__}:{exc}")
                    continue
                parsed_bytes = _canonical_json(parsed)
                relative = Path("entries") / source / str(date) / "parsed.json"
                target = temporary / relative
                _write_immutable(target, parsed_bytes)
                parsed_by_date[date][source] = parsed
                exclusions.extend({"source": source, **item} for item in local_exclusions)
                for row in parsed["rows"]:
                    key = (row["market"], row["security_code"])
                    entry = security_observations.setdefault(key, {
                        "market": row["market"], "security_code": row["security_code"],
                        "security_type": row["security_type"], "names": set(),
                        "first_source_date": date, "last_source_date": date,
                    })
                    entry["names"].add(row["security_name"])
                    entry["first_source_date"] = min(entry["first_source_date"], date)
                    entry["last_source_date"] = max(entry["last_source_date"], date)
                parse_events.append({
                    "schema_version": PARSER_SCHEMA_VERSION,
                    "identity_schema_version": IDENTITY_SCHEMA_VERSION,
                    "source": source,
                    "market": parsed["market"],
                    "feature_family": parsed["feature_family"],
                    "source_date": date,
                    "raw_reference": str(raw_path.relative_to(runtime)),
                    "raw_sha256": raw_sha,
                    "parsed_reference": str((Path("parsed_v2") / relative)),
                    "parsed_sha256": _sha256_bytes(parsed_bytes),
                    "source_row_count": parsed["source_row_count"],
                    "matched_row_count": parsed["matched_row_count"],
                    "security_type_mapping_complete_for_used_rows": True,
                })
        if parse_errors:
            raise RuntimeError(f"v2 parse errors={len(parse_errors)} first={parse_errors[:3]}")
        if len(parse_events) != expected_pairs:
            raise RuntimeError(f"v2 parsed coverage={len(parse_events)}/{expected_pairs}")

        collision_rows: list[dict] = []
        records = []
        matched_rows = 0
        source_counts = {source: 0 for source in SOURCE_ORDER}
        for date in dates:
            by_family: dict[str, dict[str, dict]] = {
                "INSTITUTIONAL": {}, "MARGIN_SHORT": {},
            }
            for source in SOURCE_ORDER:
                parsed = parsed_by_date[date][source]
                source_counts[source] += 1
                matched_rows += len(parsed["rows"])
                target = by_family[parsed["feature_family"]]
                for row in parsed["rows"]:
                    code = row["security_code"]
                    if code in target:
                        collision_rows.append({
                            "source_date": date,
                            "security_code": code,
                            "security_type": row["security_type"],
                            "first_market": target[code]["market"],
                            "second_market": row["market"],
                            "feature_family": parsed["feature_family"],
                        })
                        continue
                    target[code] = row
            for research_code in sorted(needed_codes_by_date[date]):
                code = str(int(research_code))
                inst = by_family["INSTITUTIONAL"].get(code)
                margin = by_family["MARGIN_SHORT"].get(code)
                records.append((
                    date, research_code,
                    np.nan if inst is None else inst["foreign"],
                    np.nan if inst is None else inst["investment_trust"],
                    np.nan if inst is None else inst["dealer"],
                    np.nan if margin is None else margin["margin_balance"],
                    np.nan if margin is None else margin["short_balance"],
                    0 if inst is None else (1 if inst["market"] == "TWSE" else 2),
                    0 if margin is None else (1 if margin["market"] == "TWSE" else 2),
                ))
        if collision_rows:
            raise RuntimeError(f"v2 cross-market exact identity collisions={len(collision_rows)}")

        parsed_manifest_bytes = b"".join(
            _canonical_json(event) + b"\n" for event in parse_events
        )
        _write_immutable(temporary / "parsed_manifest.jsonl", parsed_manifest_bytes)
        security_master = {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "mapping_basis": (
                "Exact string membership in the frozen four-digit common-stock research universe, "
                "then official source market identity on each source date; no numeric suffix matching."
            ),
            "frozen_research_codes_sha256": _sha256_bytes(_canonical_json({
                str(date): sorted(str(int(code)) for code in needed_codes_by_date[date])
                for date in dates
            })),
            "used_identities": [
                {**{key: value for key, value in item.items() if key != "names"}, "names": sorted(item["names"])}
                for item in sorted(security_observations.values(), key=lambda row: (row["market"], row["security_code"]))
            ],
            "official_excluded_identities": list(OFFICIAL_ETF_MASTER.values()),
        }
        security_master_bytes = _canonical_json(security_master)
        _write_immutable(temporary / "security_master.json", security_master_bytes)
        duplicate_audit = {
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "status": "PASS" if not collision_rows else "FAIL_CLOSED",
            "cross_market_identity_collisions": len(collision_rows),
            "former_cross_market_collision_occurrences": sum(
                item["security_code"] in {"006203", "006204", "006207"}
                for item in exclusions
            ),
            "legacy_numeric_false_match_exclusions": len(exclusions),
            "etf_exclusions": len(exclusions),
            "excluded_by_security_code": {
                code: sum(item["security_code"] == code for item in exclusions)
                for code in sorted({item["security_code"] for item in exclusions})
            },
            "collisions": collision_rows,
        }
        duplicate_audit_bytes = _canonical_json(duplicate_audit)
        _write_immutable(temporary / "cross_market_duplicate_audit_v2.json", duplicate_audit_bytes)

        dtype = np.dtype([
            ("source_date", "<i4"), ("stock_code", "<i4"),
            ("foreign", "<f8"), ("investment_trust", "<f8"), ("dealer", "<f8"),
            ("margin_balance", "<f8"), ("short_balance", "<f8"),
            ("institutional_market", "u1"), ("margin_market", "u1"),
        ])
        table = np.asarray(records, dtype=dtype)
        keys = np.column_stack((table["source_date"], table["stock_code"]))
        duplicate_stock_dates = len(keys) - len(np.unique(keys, axis=0))
        if duplicate_stock_dates:
            raise RuntimeError(f"v2 duplicate stock-date={duplicate_stock_dates}")
        former_cross_market_collisions = sum(
            item["security_code"] in {"006203", "006204", "006207"}
            for item in exclusions
        )
        if former_cross_market_collisions != 365:
            raise RuntimeError(
                "unexpected former cross-market collision exclusion count="
                f"{former_cross_market_collisions} expected=365"
            )
        if len(exclusions) != 641:
            raise RuntimeError(f"unexpected total v2 ETF exclusion count={len(exclusions)} expected=641")

        # Publish the complete parsed tree atomically before assembling immutable final artifacts.
        temporary.rename(v2_cache)
        parsed_manifest_sha256 = sha256_path(v2_cache / "parsed_manifest.jsonl")
        security_master_sha256 = sha256_path(v2_cache / "security_master.json")
        duplicate_audit_sha256 = sha256_path(v2_cache / "cross_market_duplicate_audit_v2.json")

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".chip_daily_store_v2.", suffix=".npz", dir=runtime
        )
        os.close(descriptor)
        store_temp = Path(temporary_name)
        try:
            np.savez_compressed(store_temp, chip_daily=table)
            _write_immutable(output, store_temp.read_bytes())
        finally:
            store_temp.unlink(missing_ok=True)

        raw_tree_after = tree_digest(raw_paths, v1_cache)
        old_parsed_tree_after = tree_digest(old_parsed_paths, v1_cache)
        if raw_tree_before != raw_tree_after:
            raise RuntimeError("immutable raw tree changed during v2 rebuild")
        if old_parsed_tree_before != old_parsed_tree_after:
            raise RuntimeError("legacy parsed forensic tree changed during v2 rebuild")
        parser_code_sha256 = sha256_path(Path(__file__))
        final_store_sha256 = sha256_path(output)
        final_manifest = {
            "status": "COMPLETE",
            "study_id": "CHIP_INCREMENTAL_STUDY_V0_1",
            "phase": "PHASE_1_ACQUISITION_FINAL_V2",
            "official_only": True,
            "parser_schema_version": PARSER_SCHEMA_VERSION,
            "identity_schema_version": IDENTITY_SCHEMA_VERSION,
            "source_raw_manifest_sha256": source_manifest_sha256,
            "raw_tree_sha256": raw_tree_after,
            "legacy_parsed_tree_sha256": old_parsed_tree_after,
            "parsed_v2_manifest_sha256": parsed_manifest_sha256,
            "parser_code_sha256": parser_code_sha256,
            "source_git_commit": _git_head(repo),
            "security_master_sha256": security_master_sha256,
            "duplicate_audit_sha256": duplicate_audit_sha256,
            "final_store_sha256": final_store_sha256,
            "counts": {
                "raw_pairs": expected_pairs,
                "parsed_v2_pairs": len(parse_events),
                "complete_dates": len(dates),
                "source_counts": source_counts,
                "cross_market_identity_collisions": 0,
                "etf_exclusions": len(exclusions),
                "ordinary_stock_matched_rows": matched_rows,
                "final_store_records": len(table),
            },
            "coverage_gate": {
                "completed_source_date_pairs": expected_pairs == 5864,
                "complete_dates": len(dates) == 1466,
                "four_sources_each_1466": all(value == 1466 for value in source_counts.values()),
                "missing_pairs": 0,
                "unresolved_parse_errors": 0,
                "integrity_hash_errors": 0,
                "cross_market_identity_collisions": 0,
                "duplicate_stock_date": duplicate_stock_dates,
                "pit_lag_mapping_complete": set(dates) == set(needed_codes_by_date),
                "security_type_mapping_complete_for_used_rows": True,
                "pass": True,
            },
            "safety": {
                "official_raw_redownloads": 0,
                "immutable_raw_modified": False,
                "legacy_parsed_modified": False,
                "formal_model_run_count": 0,
                "actual_orders": 0,
                "actual_fills": 0,
                "broker_connections": 0,
            },
            "created_at_utc": _utc_now(),
        }
        _write_immutable(final_manifest_path, json.dumps(final_manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        return final_manifest
    finally:
        if temporary.exists():
            for path in sorted(temporary.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            temporary.rmdir()


__all__ = [
    "IDENTITY_SCHEMA_VERSION", "OFFICIAL_ETF_MASTER", "PARSER_SCHEMA_VERSION",
    "parse_raw_source_v2", "rebuild_v2_from_immutable_raw", "tree_digest",
]
