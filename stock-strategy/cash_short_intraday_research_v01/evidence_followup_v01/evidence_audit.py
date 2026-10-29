"""Read-only evidence audit, never a strategy or broker runner.

Only an explicitly new output directory is writable. Archive bytes and all old
research files are opened read-only. No SDK is loaded. Public documentation may
be fetched with --fetch-official; its hashes, not authentication examples, are
retained. This module does not promote any unverified quantity or fee assumption.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


SCHEMA = "CASH_SHORT_EVIDENCE_FOLLOWUP_V01"
OFFICIAL_URLS = {
    "sdk_stocktick": "https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/行情/分時明細訂閱/index.html",
    "sdk_fivelevel": "https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/行情/最佳五檔行情訂閱/index.html",
    "sdk_enums": "https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/基礎/列舉物件/index.html",
    "public_fee": "https://www.yuanta.com.tw/eYuanta/Securities/Node/Index?C1=2021102007380647&C2=2021102008672547&ID=2021102008672547&Level=2&MainId=00415",
    "tax_mof": "https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/tax-knowledge/rwG2M1N",
    "tax_2025_revision": "https://www.dot.gov.tw/singlehtml/ch26?cntId=fde9d7d1546d4df8a4db96bf23e2f57c",
}
SAFE_HOSTS = {"www.yuanta.com.tw", "www.etax.nat.gov.tw", "www.dot.gov.tw"}
WATCHED_FIELDS = (
    "received_at", "callback_received_at", "quote_time", "exchange_time",
    "written_at", "write_time", "persisted_at", "ingest_sequence", "serial_no",
    "deal_volume", "buy_volumes", "sell_volumes", "raw_volume_unit",
    "volume_unit", "sdk_version", "collector_sha256", "subscription_generation",
)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def extract_sdk_version_candidates(strings_text):
    """Identify date-style SDK version strings without pinning one release.

    These are visible binary string candidates, not authenticated PE metadata or
    historical runtime attestations. Multiple candidates remain ambiguous.
    """
    pattern = r"(?<![\w.])(?:\d+\.\d{4}\.\d{4}\.\d+|\d{4}\.\d{1,2}\.\d{1,2}\.\d+)(?![\w.])"
    return sorted(set(re.findall(pattern, str(strings_text))))


def sdk_evidence_description(sources, manifest_rows=None):
    """Render observed package/version and manifest coverage from metadata."""
    declared = sources.get("sdk_deps", {}).get("declared_package_versions", [])
    declared_text = ", ".join(str(x) for x in declared) if declared else "UNKNOWN"
    dll = sources.get("sdk_dll", {})
    candidates = dll.get("visible_version_candidates")
    if candidates is None:
        # Existing private audit payloads predate the candidate-list field.
        prior = dll.get("visible_file_version_string")
        candidates = [prior] if prior and not str(prior).startswith("UNKNOWN") else []
    candidate_text = ", ".join(str(x) for x in candidates) if candidates else "UNKNOWN"
    metadata = sources.get("source_manifests", {})
    count = len(manifest_rows) if manifest_rows is not None else metadata.get("count", "UNKNOWN")
    sdk_count = (sum(bool(x.get("sdk_version_field_present")) for x in manifest_rows)
                 if manifest_rows is not None else metadata.get("sdk_version_field_present_count", "UNKNOWN"))
    code_count = (sum(bool(x.get("collector_source_hash_field_present")) for x in manifest_rows)
                  if manifest_rows is not None else metadata.get("collector_source_hash_field_present_count", "UNKNOWN"))
    return (f"deps宣告{declared_text}；DLL可見版本候選{candidate_text}，另計各檔SHA。"
            f"{count}份manifest中SDK版本/雜湊欄存在{sdk_count}份、collector原碼雜湊欄存在{code_count}份。"
            "目前套件識別不等於歷史當時使用版本；版本綁定、文件日期與官網版本詳來源metadata，未證者保留UNKNOWN。")


def _stamp(text):
    d = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("NAIVE_TIMESTAMP")
    return d


def quantity_provenance(raw_value, *, raw_unit="UNKNOWN", semantic="UNKNOWN",
                        target_unit="SHARES", multiplier=None, basis=None,
                        basis_sha256=None, certified=False, source_field=None):
    """New sidecar metadata; no inference from magnitude and no old-file edits.

    Unknown or scenario quantities may not populate certified converted_value.
    Repassing this metadata as a raw scalar is rejected, preventing a second
    multiplication. Exact raw strings are retained, as is explicit evidence.
    """
    if isinstance(raw_value, (dict, list, tuple, bool)) or raw_value is None:
        raise ValueError("RAW_SCALAR_REQUIRED_NO_DOUBLE_CONVERSION")
    try:
        value = Decimal(str(raw_value))
    except InvalidOperation:
        raise ValueError("INVALID_RAW_QUANTITY") from None
    if not value.is_finite() or value < 0:
        raise ValueError("INVALID_RAW_QUANTITY")
    raw_unit, target_unit = str(raw_unit), str(target_unit)
    out = {
        "schema_version": SCHEMA, "source_field": source_field,
        "raw_value": str(raw_value), "raw_unit": raw_unit,
        "semantic": str(semantic), "target_unit": target_unit,
        "converted_value": None, "conversion_multiplier": None,
        "conversion_basis": basis, "conversion_basis_sha256": basis_sha256,
        "conversion_status": "UNKNOWN_NOT_CONVERTED", "conversion_applied": False,
    }
    if not certified:
        if multiplier is not None:
            factor = Decimal(str(multiplier))
            if not factor.is_finite() or factor <= 0:
                raise ValueError("INVALID_MULTIPLIER")
            out.update(scenario_multiplier=str(factor),
                       scenario_value=str(value * factor),
                       conversion_status="CONDITIONAL_SCENARIO_NOT_CERTIFIED")
        return out
    if raw_unit not in {"SHARES", "BOARD_LOTS"} or target_unit != "SHARES":
        raise ValueError("CERTIFIED_UNIT_UNSUPPORTED")
    if (not basis or not basis_sha256 or len(str(basis_sha256)) != 64
            or any(c not in "0123456789abcdef" for c in str(basis_sha256).lower())):
        raise ValueError("CERTIFICATION_REQUIRES_TRACEABLE_BASIS")
    expected = Decimal(1 if raw_unit == "SHARES" else 1000)
    if multiplier is not None and Decimal(str(multiplier)) != expected:
        raise ValueError("UNIT_MULTIPLIER_CONFLICT")
    out.update(converted_value=str(value * expected),
               conversion_multiplier=str(expected), conversion_applied=True,
               conversion_status="CERTIFIED_EXPLICIT_CONVERSION")
    return out


def timestamp_provenance(row):
    """Never substitute a receipt timestamp for a missing exchange timestamp."""
    return {
        "exchange_time_raw": row.get("exchange_time") or row.get("quote_time"),
        "exchange_time_status": "ARCHIVED_FIELD_REQUIRES_CLOCK_BINDING" if (
            row.get("exchange_time") or row.get("quote_time")) else "UNKNOWN_NOT_RECOVERABLE_FROM_RECEIPT",
        "received_at": row.get("received_at"),
        "callback_received_at": row.get("callback_received_at"),
        "write_at": row.get("written_at") or row.get("write_time") or row.get("persisted_at"),
        "ingest_sequence": row.get("ingest_sequence"),
        "serial_no": row.get("serial_no"),
        "serial_scope": "PER_SYMBOL_STOCKTICK_ONLY_NOT_CROSS_CHANNEL",
        "transport_minus_exchange_seconds_is_order_latency": False,
    }


def scan_stream(path, *, day, role, expected_sha256):
    """Count safe schema evidence only; no raw quote/account payload is exported."""
    path = Path(path)
    before = path.stat()
    actual = sha256(path)
    if actual != expected_sha256:
        raise ValueError("SOURCE_HASH_MISMATCH:" + path.name)
    counts, flag_counts = Counter(), Counter()
    n = 0
    volume_last, serial_last, receipt_last = {}, {}, None
    changes, serial_steps, volume_semantics = Counter(), Counter(), Counter()
    seq_last, seq_regressions, receipt_regressions = None, 0, 0
    first_receipt, last_receipt = None, None
    raw_sample = None
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            n += 1
            for field in WATCHED_FIELDS:
                if field in row and row[field] is not None:
                    counts[field] += 1
            receipt = row.get("received_at")
            if receipt:
                now = _stamp(receipt)
                if receipt_last is not None and now < receipt_last:
                    receipt_regressions += 1
                receipt_last = now
                first_receipt = receipt if first_receipt is None else first_receipt
                last_receipt = receipt
            seq = row.get("ingest_sequence")
            if isinstance(seq, int) and not isinstance(seq, bool):
                if seq_last is not None and seq < seq_last:
                    seq_regressions += 1
                seq_last = seq
            symbol = str(row.get("stock_id", ""))
            flag_counts[str(row.get("index_flag") if role.endswith("books") else row.get("in_out_flag"))] += 1
            if role.endswith("ticks"):
                try:
                    serial, flag = int(row.get("serial_no", 0)), str(row.get("in_out_flag", ""))
                    volume = Decimal(str(row["deal_volume"]))
                except (ValueError, TypeError, KeyError, InvalidOperation):
                    volume_semantics["unreadable_tick"] += 1
                    continue
                if serial < 1 or flag not in {"0", "1", "2", "3"}:
                    volume_semantics["header_or_status_not_trade"] += 1
                    continue
                volume_semantics["ordinary_trade_message"] += 1
                if symbol in volume_last:
                    changes["down" if volume < volume_last[symbol] else "up" if volume > volume_last[symbol] else "equal"] += 1
                volume_last[symbol] = volume
                if symbol in serial_last:
                    delta = serial - serial_last[symbol]
                    serial_steps["regression" if delta < 0 else "duplicate" if delta == 0 else "gap" if delta > 1 else "consecutive"] += 1
                serial_last[symbol] = serial
                if raw_sample is None:
                    raw_sample = {
                        "source_line": n, "source_field": "DealVol -> deal_volume",
                        "raw_value": str(row["deal_volume"]), "normalized_numeric_value": str(volume),
                        "raw_unit": "UNKNOWN_SDK_NATIVE", "multiplier_before_execution": 1,
                        "serial_no": serial, "timestamp_provenance": timestamp_provenance(row),
                    }
            elif raw_sample is None:
                raw_sample = {
                    "source_line": n, "source_field": "IndexFlag_50 BuyVol[1..5]/SellVol[1..5]",
                    "raw_buy_volumes": row.get("buy_volumes"),
                    "raw_sell_volumes": row.get("sell_volumes"),
                    "raw_unit": "UNKNOWN_SDK_NATIVE", "multiplier_before_execution": 1,
                    "timestamp_provenance": timestamp_provenance(row),
                }
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("SOURCE_CHANGED_DURING_READ:" + path.name)
    return {
        "day": day, "role": role, "path": str(path), "sha256": actual,
        "source_hash_matches_round2_precheck": True, "record_count": n,
        "field_present_counts": dict(counts), "flag_counts": dict(flag_counts),
        "first_receipt": first_receipt, "last_receipt": last_receipt,
        "receipt_regression_count_in_original_file_order": receipt_regressions,
        "known_ingest_sequence_regression_count": seq_regressions,
        "native_volume_message_changes": dict(changes),
        "tick_message_semantics": dict(volume_semantics),
        "tick_serial_steps_per_symbol_original_file_order": dict(serial_steps),
        "serial_gaps_prove_network_outage": False,
        "decreasing_native_volume_proves_single_execution_quantity": False,
        "unit_certification": "UNKNOWN", "safe_raw_to_normalized_example": raw_sample,
        "stable_during_read": True,
    }


def fetch_official(url):
    encoded = quote(url, safe=":/?=&%")
    if urlsplit(encoded).hostname not in SAFE_HOSTS:
        raise ValueError("DOCUMENT_HOST_NOT_ALLOWLISTED")
    now = datetime.now(timezone.utc).isoformat()
    try:
        with urlopen(Request(encoded, headers={"User-Agent": "Mozilla/5.0 evidence-read-only"}), timeout=30) as r:
            if urlsplit(r.url).hostname not in SAFE_HOSTS:
                raise ValueError("DOCUMENT_REDIRECT_OUTSIDE_ALLOWLIST")
            body = r.read(5_000_001)
            if len(body) > 5_000_000:
                raise ValueError("DOCUMENT_TOO_LARGE")
            return {"url": url, "final_url": r.url, "retrieved_at_utc": now,
                    "status": "FETCHED_PUBLIC_DOCUMENT_NO_BROKER_SESSION",
                    "content_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
                    "last_modified_header": r.headers.get("Last-Modified"),
                    "etag_header": r.headers.get("ETag"),
                    "document_release_version": "UNKNOWN_UNVERSIONED_HTML",
                    "raw_body_retained": False,
                    "privacy": "RAW_AUTHENTICATION_EXAMPLES_NOT_EXPORTED"}
    except Exception as exc:
        return {"url": url, "retrieved_at_utc": now,
                "status": "FETCH_UNCONFIRMED", "error_type": type(exc).__name__,
                "content_sha256": None, "document_release_version": "UNKNOWN"}


def fee_db_aggregate(path):
    """Read schema and fee-key counts only, including committed WAL contents.

    The caller must first bind this path to the installed project's LaunchAgent.
    Never SELECT an arbitrary payload, account number, order number or position.
    There are no write SQL statements and no SDK imports or broker connections.
    """
    path = Path(path).resolve()
    files = [p for p in (path, Path(str(path) + "-wal")) if p.is_file()]
    before = {p.name: sha256(p) for p in files}
    result = {"path": str(path), "sqlite_uri_mode": "ro", "query_only": True,
              "immutable": False, "wal_aware_read_transaction": True,
              "schema": {}, "record_counts": {}, "fee_key_counts": {},
              "private_identifiers_exported": False,
              "raw_payloads_exported": False, "database_records_modified": False,
              "record_origin": "NOT_CERTIFIED_ACTUAL_BROKER_CHARGES"}
    # URI quoting retains literal '?' and '#' in a filesystem path.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result["data_version_start"] = connection.execute("PRAGMA data_version").fetchone()[0]
        present = {x[0] for x in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("live_orders", "live_fills", "broker_result_receipts", "live_events", "position_checkpoints"):
            if table not in present:
                continue
            cols = [r[1] for r in connection.execute('PRAGMA table_info("' + table + '")')]
            result["schema"][table] = cols
            result["record_counts"][table] = connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            if table in {"broker_result_receipts", "live_events", "position_checkpoints"} and "payload" in cols:
                # json_tree traverses in SQLite. Python receives only the
                # allowlisted fee/tax key names and counts, never their values.
                query = ("SELECT jt.key,count(*),sum(CASE WHEN jt.type IN ('integer','real') THEN 1 ELSE 0 END) "
                         "FROM " + table + " t,json_tree(CASE WHEN json_valid(t.payload) THEN t.payload ELSE '{}' END) jt "
                         "WHERE jt.key IN ('fees','fee','tax','commission','OCharge','OTax','commission_rate',"
                         "'commission_rate_each_side','minimum_commission_twd') GROUP BY jt.key")
                result["fee_key_counts"][table] = [
                    {"field_name": key, "occurrences": count, "numeric_occurrences": numeric}
                    for key, count, numeric in connection.execute(query)
                ]
        result["data_version_end_in_transaction"] = connection.execute("PRAGMA data_version").fetchone()[0]
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    after = {p.name: sha256(p) for p in files}
    result["source_sha256_before"] = before
    result["source_sha256_after"] = after
    result["persistent_database_and_wal_bytes_unchanged_during_read"] = before == after
    result["actual_fee_evidence_found"] = any(
        r["occurrences"] for rs in result["fee_key_counts"].values() for r in rs)
    result["actual_account_fee"] = "UNKNOWN_REQUIRES_FEE_VALUES_AND_TRANSACTION_BASIS" if result["actual_fee_evidence_found"] else "UNKNOWN_NO_FEE_FIELDS_IN_SCOPED_LEDGER"
    return result


def audit_named_external_fee_ledger(root, ledger_inventory, *, fee_launchagent):
    """Follow exactly one inventoried project runtime, never scan the disk."""
    root = Path(root).resolve()
    external = ledger_inventory.get("external_runtime", {})
    runtime = Path(str(external.get("path", "/nonexistent"))).resolve()
    candidates = [r for r in external.get("safe_file_metadata", [])
                  if r.get("filename") == "live-orders.sqlite"]
    result = {"scope": "ONE_NAMED_LEDGER_BOUND_TO_THIS_PROJECT_INSTALLED_LAUNCHAGENT",
              "external_strategy_results_or_settings_used": False,
              "credential_files_opened": False, "account_identifiers_exported": False,
              "raw_payloads_exported": False, "broker_login": False,
              "actual_account_fee": "UNKNOWN", "scope_binding_status": "UNCONFIRMED"}
    # Deployment identifiers are private inputs, never public source constants.
    plist = Path(fee_launchagent).resolve()
    if len(candidates) != 1 or not plist.is_file():
        result["scope_binding_failure"] = "NAMED_LEDGER_OR_INSTALLED_PLIST_NOT_UNIQUE"
        return result
    item = candidates[0]
    target = Path(item["path"]).resolve()
    data = plistlib.loads(plist.read_bytes())
    arguments = data.get("ProgramArguments", [])
    positions = [i for i, x in enumerate(arguments) if x == "--runtime-dir"]
    runtime_arg = arguments[positions[0] + 1] if len(positions) == 1 and positions[0] + 1 < len(arguments) else None
    valid = (Path(str(data.get("WorkingDirectory", ""))).resolve() == root / "stock-strategy"
             and runtime_arg is not None and Path(runtime_arg).resolve() == runtime
             and target.parent == runtime and target.name == "live-orders.sqlite")
    result["scope_binding_status"] = "CONFIRMED_INSTALLED_PROJECT_RUNTIME_LINK" if valid else "UNCONFIRMED"
    result["binding_evidence"] = {"plist_path": str(plist), "plist_sha256": sha256(plist),
                                  "working_directory_matches_this_project": valid,
                                  "runtime_argument_matches_inventory": runtime_arg is not None and Path(runtime_arg).resolve() == runtime,
                                  "inventory_declared_path": str(target)}
    if not valid or not target.is_file():
        result["scope_binding_failure"] = "INSTALLED_RUNTIME_MISMATCH_OR_DATABASE_ABSENT"
        return result
    external_root = runtime.parents[2]
    proc = subprocess.run(["git", "-C", str(external_root), "rev-parse", "HEAD"], capture_output=True, text=True)
    result["external_checkout_head_metadata_only"] = proc.stdout.strip() if proc.returncode == 0 else "UNKNOWN"
    result["fee_ledger_aggregate"] = fee_db_aggregate(target)
    result["actual_account_fee"] = result["fee_ledger_aggregate"]["actual_account_fee"]
    result["account_database_queried"] = True
    return result


def augment_existing_fee_evidence(root, output, *, fee_launchagent):
    """Extend only this new follow-up output; old research is never a target."""
    root, output = Path(root).resolve(), Path(output).resolve()
    allowed = root / "stock-strategy/cash_short_intraday_research_v01/evidence_followup_v01"
    if not output.is_relative_to(allowed) or output == allowed:
        raise ValueError("OUTPUT_MUST_BE_NEW_FOLLOWUP_RESULTS_DIRECTORY")
    dest = output / "evidence_tables.json"
    result = json_read(dest)
    if result["schema_version"] != SCHEMA or result.get("named_external_fee_ledger_audit"):
        raise ValueError("NOT_UNAUGMENTED_FOLLOWUP_EVIDENCE")
    original_sha = sha256(dest)
    inventory_path = root / "stock-strategy/cash_short_intraday_research_v01/results_round1_v2/LEDGER_INVENTORY.json"
    audit = audit_named_external_fee_ledger(root, json_read(inventory_path),
                                           fee_launchagent=fee_launchagent)
    result["evidence_augmented_from_sha256"] = original_sha
    result["evidence_augmented_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["named_external_fee_ledger_audit"] = audit
    fee_inventory = result["sources"]["fee_inventory"]
    fee_inventory.update({"scope": "CURRENT_CHECKOUT_PLUS_ONE_EXPLICIT_NAMED_EXTERNAL_LEDGER_BOUND_TO_INSTALLED_LAUNCHAGENT",
                          "external_checkout_private_runtime_opened": audit.get("account_database_queried", False),
                          "account_db_queried": audit.get("account_database_queried", False),
                          "actual_account_fee": audit["actual_account_fee"],
                          "limitation": "Only schema, table counts and allowlisted JSON fee/tax-key existence counts were read. No account/order IDs, quote/trade prices, quantities or arbitrary payloads were exported; a fill is not fee evidence."})
    for row in result["evidence_audit"]:
        if row["item_id"] == "B01_COMMISSION_RATE":
            row["actual_evidence"] = "研究config與舊SPEC相同；元大公開費用頁支持牌告及最低20但非本帳戶約定。另按具名LaunchAgent確認外部帳戶庫屬本專案runtime，只讀含WAL的一致交易；live_fills無費用欄，限定fee/tax JSON鍵查核未找到可驗證實際費率的對帳。既有委託/成交紀錄不直接當費用認證。"
        if row["item_id"] == "B02_FEE_GRANULARITY_ROUNDING":
            row["actual_evidence"] += " 具名帳戶庫的schema與限定fee/tax-key計數也不足確認最低費粒度或實際取整；不擷取任意broker payload。"
    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_report(result, output)
    print("EVIDENCE FEE AUGMENTATION COMPLETE", audit["scope_binding_status"], audit["actual_account_fee"], "actual_orders=0")
    return result


def build_evidence_rows(source_ids):
    def row(item, category, assumption, evidence, status, impact, correction, refs, **extra):
        return {"item_id": item, "category": category, "existing_assumption": assumption,
                "actual_evidence": evidence, "confirmation_level": status,
                "affected_scope": impact, "correction_or_next_evidence": correction,
                "source_ids": refs, "model_parameter_change_supported": False, **extra}
    return [
        row("A01_DEALVOL_UNIT", "VOLUME_UNIT", "DealVol保留SDK原生量；模型未認證股/張。", "本機SDK/官方HTML僅列成交量；千股備註限定海外期貨現貨。未找到台股DealVol股/張明文。", "UNKNOWN", "VWAP/flow量之語義、P90大單定義；絕對量和容量認證。", "須同SDK版本且指定TWSE/TWOTC逐筆欄位的原廠定義；不得依乘1000後績效或數量大小認證。", ["sdk_stocktick_local", "sdk_stocktick", "sdk_deps"]),
        row("A02_BID_DEPTH_UNIT", "VOLUME_UNIT", "BASE/WORSE把五檔買量視為張×1000；另保留股×1。", "SDK FiveTick_Flag50/51 BuyVol1..5為Int買量，未寫股或張；collector僅str，normalizer僅float。", "UNKNOWN", "空方進場買方深度、部分成交、可負擔量與成交率。", "原廠對TWSE/TWOTC五檔買量的單位證明；若修正須所有比較組共同重跑。", ["sdk_fivelevel_local", "sdk_fivelevel", "collector_main", "normalizer", "execution"]),
        row("A03_ASK_DEPTH_UNIT", "VOLUME_UNIT", "BASE/WORSE把五檔賣量視為張×1000；另保留股×1。", "SDK FiveTick_Flag50/51 SellVol1..5為Int賣量，未寫股或張；轉換路徑同買量但不可視為已認證。", "UNKNOWN", "回補賣方容量、未回補與未平曝險。", "須獨立明示賣量欄位單位，不以買方證據或交易整張單位推定。", ["sdk_fivelevel_local", "sdk_fivelevel", "collector_main", "normalizer", "execution"]),
        row("A04_DEALVOL_SEMANTICS", "VOLUME_SEMANTICS", "每一StockTickResult.DealVol作事件量，不作累積量差分。", "文件定義分時明細及每股票SerialNo；封存一般成交DealVol可下降，排除整日單調累積值假說，但未證實每訊息等於單一撮合或無快照/彙整/重送。", "PARTIAL_NOT_SINGLE_FILL_CERTIFIED", "60秒flow、300秒大單參考與VWAP的經濟語義。", "原廠明示是否逐筆成交、撮合彙總、快照及補發；同日完整交易所明細校驗。現僅維持事件量條件模型。", ["sdk_stocktick_local", "sdk_stocktick", "raw_schema_scan", "normalizer"]),
        row("A05_BOOK_SEMANTICS", "VOLUME_SEMANTICS", "五檔量為各價格檔當次顯示剩餘委買/賣量，不相加成交易量。", "API明定最佳五檔/各檔價量欄位；本研究每個book以當次levels更新，不把callback相加，也不把同價同量新callback當新容量。", "CONFIRMED_SCHEMA_CURRENT_DOC_HISTORICAL_BINDING_UNKNOWN", "可確認模型如何消耗觀察深度，不能證明排隊成交或隱藏量。", "歷史SDK綁定及交易所時鐘、order queue仍需補強；容量10%本身仍是假設。", ["sdk_fivelevel_local", "sdk_fivelevel", "execution"]),
        row("A06_TRANSFORM_PATH", "TRANSFORM_PROVENANCE", "正規化無量單位轉換；僅執行模擬套用情境乘數。", "collector DealVol/BuyVol/SellVol->str原值->JSONL；data._events float但payload保留原str；execution容量depth×multiplier×0.1後floor整張。", "CONFIRMED_CODE_PATH_NOT_UNIT_CERTIFICATION", "可定位重複換算風險與需修正的唯一容量層；不改第一/二輪。", "新sidecar保留raw_value/raw_unit/semantic/target_unit/multiplier/basis SHA；unknown時converted_value=null；情境值另欄。", ["collector_main", "collector", "normalizer", "execution"]),
        row("A07_SDK_VERSION", "VERSION_BINDING", "本機目前SDK可能與封存當時相同，不能憑路徑確認。", sdk_evidence_description(source_ids), "CURRENT_PACKAGE_IDENTIFIED_HISTORICAL_UNKNOWN", "全部歷史欄位語義與serializer實作的版本連結。", "後續由既有封存流程另經授權綁DLL/source/docs SHA；本次不改collector與排程，不能回填歷史版本。", ["sdk_deps", "sdk_dll", "sdk_stocktick_local", "sdk_fivelevel_local", "source_manifests"]),
        row("B01_COMMISSION_RATE", "FEE", "每邊0.000855，最低20元。", "研究config及舊SPEC相同；元大公開費用頁列0.001425與電子折扣/每筆最低20，公開資料支持代理值的合理來源但不是本帳戶約定。checkout未見正式runtime或實際券商交割對帳檔。", "PROXY_SETTING_CONFIRMED_ACTUAL_ACCOUNT_UNKNOWN", "全部成交淨損益、初始R、部位與後續權益路徑。", "僅接受已封存/使用者提供去識別實際費率約定或含成交金額與費用對帳，不登入券商。", ["round1_config", "legacy_fee_spec", "public_fee", "fee_inventory"]),
        row("B02_FEE_GRANULARITY_ROUNDING", "FEE", "每ENTRY/EXIT委託累積成交額各收max(20,ceil(value×rate))；不按五檔分層重收。", "execution.cost/replay.fee/mark source明確；現有live accounting也明說估算非券商結算。broker adapter具歷史OCharge/OTax mapping，不表示現存實際回報。", "MODEL_ARITHMETIC_CONFIRMED_ACTUAL_BILLING_UNKNOWN", "最低費、拆單/多次回補費用、逐元取整與稅額。", "實際帳務需同一委託多次成交、跨委託及日結粒度與取整/退佣規則證據；當前維持代理。", ["execution", "replay", "current_accounting", "broker_history_adapter", "fee_inventory"]),
        row("B03_DAYTRADE_TAX", "TAX", "同日完成之合格現股當沖股票賣出額×0.0015。", "財政部2025/01/02修法說明及稅務入口更新2025/08/11，符合條件之股票當沖1.5‰延至2027/12/31；此確認法定情境稅率，不確認帳戶/每筆交易已合格或實際代徵取整。", "STATUTORY_CONDITIONAL_RATE_CONFIRMED_ACCOUNT_APPLICATION_UNKNOWN", "既有研究有效日期中已完成合格當沖之稅率設定未因這項法律情境確認而改變。未回補不可據此認定完整費用。", "不重跑相同設定；未平倉券差借券/失敗交割/額外費用仍UNKNOWN，需實際可讀帳務證據。", ["tax_mof", "tax_2025_revision", "round1_config", "execution"]),
        row("B04_EXTRA_SHORT_COSTS", "FEE", "正常完成當沖僅proxy雙邊佣金＋賣出稅；額外券差成本未驗證。", "研究沒有券商出借/券差/未完成回補帳務證據；不得默認借券費為0或把未回補移除。", "UNKNOWN", "未完成回補、broker entitlement與最壞風險；STRICT可執行性。", "保留已知模型成本與無法估算成本分列；不據行情補造實際費用。", ["round1_config", "fee_inventory"]),
        row("C01_EXCHANGE_TICK_CLOCK", "TIMESTAMP", "quote_time為SDK Time，日期取receipt的台北日期。", "SDK有TYuantaTime(時分秒毫秒)，collector._quote_time直接保存；原始Time不含完整日期/時區/校時證明。normalizer拼receipt台北日，非獨立交易所完整UTC時間戳。", "FIELD_PATH_CONFIRMED_CLOCK_SYNCHRONIZATION_UNKNOWN", "事件窗口、staleness、quote transport difference。", "保留raw quote_time與date inference_basis；receipt與exchange可為負差時不能直接稱網路負延遲，亦不能推order latency。", ["sdk_stocktick_local", "collector_main", "normalizer", "raw_schema_scan"]),
        row("C02_BOOK_CLOCK", "TIMESTAMP", "五檔exchange_time未知，只按received可得性研究。", "SDK FiveTickAResult schema無Time欄，collector book payload不封存交易所時間；全量raw欄位計數另列。normalizer缺exchange欄回None。", "HISTORICAL_BOOK_EXCHANGE_CLOCK_UNAVAILABLE", "無法確認book在交易所的實際新鮮度與延遲；仍可依本機已收快照做條件研究。", "歷史原資料未封存者不可補回；不得以receipt或檔案mtime冒充。未來需上游提供欄位，非本次新增訂閱。", ["sdk_fivelevel_local", "sdk_fivelevel", "collector_main", "normalizer", "raw_schema_scan"]),
        row("C03_RECEIPT_WRITE_CLOCK", "TIMESTAMP", "received_at為callback處理時datetime.now(UTC)，寫檔時間不另存。", "collector在路由後utc_now，精度milliseconds；append同步write/flush可能晚於receipt，compressed每100行flush；mtime只是檔案修改時間。全量欄位計數另列。", "RECEIPT_FIELD_CONFIRMED_WRITE_TIME_UNKNOWN", "同毫秒跨通道排序不確定性，寫檔latency不能由現存檔案回復。", "sidecar分開exchange/receipt/write，write未知維持null；不得以created/mtime反推每事件時刻。", ["collector_main", "collector", "raw_schema_scan"]),
        row("C04_SEQUENCE", "ORDERING", "SerialNo逐股票tick序號，ingest_sequence存在時才是本機接收序。", "官方SerialNo按股票從1編、-1清盤；原archive的ingest欄位覆蓋實數另列；normalizer缺欄時合成file-order tiebreak，明標synthetic，不是交易所跨channel序號。", "PER_SYMBOL_SERIAL_CONFIRMED_CROSS_CHANNEL_ORDER_UNKNOWN_WHEN_UNARCHIVED", "同毫秒tick/book先後、重複/亂序與gap判讀。", "不得用SerialNo排列全市場或tick和book；不得把synthetic當observed ingest。原始索引、receipt與欄位來源並列保留。", ["sdk_stocktick_local", "sdk_stocktick", "normalizer", "raw_schema_scan"]),
        row("C05_LATENCY", "LATENCY", "BASE1秒、WORSE3秒委託延遲是固定假設，非傳輸延遲分布。", "研究無可對應signal-submit-ack-fill的實測帳務生命週期；tick receipt−SDK clock僅含時鐘偏差及傳輸/處理且無五檔exchange clock。", "ORDER_LATENCY_UNKNOWN_SCENARIO_ONLY", "所有進出成交價、部分成交與風險；不得升級可成交證據。", "延續1/3秒壓力情境，不從行情receipt差重新估下單延遲；之後需另授權的既有事件生命週期證據。", ["round1_config", "normalizer", "ledger_inventory", "raw_schema_scan"]),
        row("C06_GAP_SEMANTICS", "DATA_GAP", "callback空窗及serial gap視品質旗標，暖機保守失效，但不宣稱斷線。", "per-symbol receipt gap可能沒有成交/五檔變更；狀態14/15是SDK交易暫停/恢復，非網路連線。callback error、run status、停跑和資料缺口必須分開。", "NO_OUTAGE_INFERENCE_FROM_CALLBACK_GAP", "有效區間/暖機與可評估性降低，不可填成0訊號或改認休市。", "需全局heartbeat、connection state與範圍完整的序號/補發證據；本次僅報觀察到間隔/狀態，不補造原因。", ["sdk_stocktick_local", "collector", "normalizer", "raw_schema_scan"]),
        row("STRICT_GATE", "CERTIFICATION", "STRICT_EXECUTABLE=NA。", "只補強了轉換路徑、欄位來源、當沖法定稅率情境與缺欄位的實數證據；量單位、歷史SDK綁定、實際費率/延遲、資格/處置/停牌/帳戶權限、五檔exchange clock與排隊未齊。", "NA_UNCHANGED", "不能宣稱paper可成交、可上線或解除SHORT限制。", "逐項充分且同日PIT可得後才可能升級；單一費率/欄位確認不會整體通過。", list(source_ids)),
    ]


def run(root, output, *, vendor_dir=None, fetch=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    allowed = root / "stock-strategy/cash_short_intraday_research_v01/evidence_followup_v01"
    if not output.is_relative_to(allowed) or output == allowed:
        raise ValueError("OUTPUT_MUST_BE_NEW_FOLLOWUP_RESULTS_DIRECTORY")
    if (output / "evidence_tables.json").exists() or (output / "EVIDENCE_AUDIT.md").exists():
        raise ValueError("REFUSE_TO_OVERWRITE_EXISTING_EVIDENCE_RESULTS")
    module = root / "stock-strategy/cash_short_intraday_research_v01"
    precheck = json_read(module / "round2/precheck.json")
    manifest = json_read(module / "round2/results_round2_v1/run_manifest.json")
    days = set(manifest["conditional_days"])
    vendor = Path(vendor_dir or root.parent / "YuantaSparkAPI_osx-arm64_Python")
    sources = {}
    local_paths = {
        "sdk_deps": vendor / "YuantaSparkAPI.deps.json",
        "sdk_dll": vendor / "YuantaSparkAPI.dll",
        "sdk_stocktick_local": vendor / "IO_Doc/SubscribeStocktick_IO_Spec.doc",
        "sdk_fivelevel_local": vendor / "IO_Doc/SubscribeFiveTickA_IO_Spec.doc",
        "collector_main": root / "stock-strategy/yuanta_intraday_shadow_v01/collector_main.py",
        "collector": root / "stock-strategy/yuanta_intraday_shadow_v01/collector.py",
        "normalizer": module / "data.py", "execution": module / "execution.py",
        "replay": module / "replay.py", "round1_config": module / "config.json",
        "legacy_fee_spec": root / "stock-strategy/yuanta_intraday_shadow_v01/direction_follow_backtest.py",
        "current_accounting": root / "stock-strategy/yuanta_live_runtime_v01/accounting.py",
        "broker_history_adapter": root / "stock-strategy/yuanta_broker_execution_v01/adapter.py",
        "ledger_inventory": module / "results_round1_v2/LEDGER_INVENTORY.json",
        "round1_tables": module / "results_round1_v2/tables.json",
        "round2_precheck": module / "round2/precheck.json",
    }
    for key, path in local_paths.items():
        sources[key] = {"path": str(path), "sha256": sha256(path) if path.is_file() else None,
                        "exists": path.is_file(), "source_date": "FILE_BYTES_CURRENT_AUDIT_NOT_HISTORICAL_ATTESTATION"}
    if local_paths["sdk_deps"].is_file():
        deps = json_read(local_paths["sdk_deps"])
        declared = sorted({name for target in deps["targets"].values() for name in target if name.startswith("YuantaSparkAPI/")})
        sources["sdk_deps"]["declared_package_versions"] = declared
    for key in ("sdk_stocktick_local", "sdk_fivelevel_local"):
        proc = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(local_paths[key])], capture_output=True, check=True)
        sources[key]["extracted_text_sha256"] = hashlib.sha256(proc.stdout).hexdigest()
        extracted_text = proc.stdout.decode("utf-8", errors="replace")
        match = re.search(r"製作日期\s*\n(.*?)\nService ID", extracted_text, re.DOTALL)
        document_date = match.group(1).strip() if match else ""
        sources[key]["embedded_document_date"] = document_date or "UNKNOWN_DOCUMENT_DATE_NOT_EXTRACTED"
        sources[key]["embedded_release_date"] = "UNKNOWN_DOCUMENT_DATE_NOT_RELEASE_ATTESTATION"
        sources[key]["release_binding_to_installed_dll"] = "UNKNOWN_NOT_PROVEN_BY_COLOCATION"
    if local_paths["sdk_dll"].is_file():
        proc = subprocess.run(["strings", str(local_paths["sdk_dll"])], capture_output=True, check=True)
        versions = extract_sdk_version_candidates(proc.stdout.decode("utf-8", errors="replace"))
        sources["sdk_dll"]["visible_version_candidates"] = versions
        sources["sdk_dll"]["visible_file_version_string"] = versions[0] if len(versions) == 1 else "UNKNOWN_NO_UNIQUE_VERSION_STRING"
        sources["sdk_dll"]["version_evidence_kind"] = "VISIBLE_BINARY_STRINGS_NOT_AUTHENTICATED_PE_VERSION"
        sources["sdk_dll"]["sdk_loaded_or_executed"] = False
    for key, url in OFFICIAL_URLS.items():
        sources[key] = fetch_official(url) if fetch else {"url": url, "status": "NO_FETCH_REQUESTED", "content_sha256": None}
    scans, source_manifests = [], []
    rows = [x for x in precheck["source_checks"] if x.get("day") in days]
    for source in rows:
        if source["role"] == "run_manifest":
            d = json_read(source["path"])
            source_manifests.append({
                "day": source["day"], "path": source["path"], "sha256": sha256(source["path"]),
                "manifest_declared_hash": d.get("manifest_hash"),
                "sdk_version_field_present": any(k in d for k in ("sdk_version", "sdk_sha256", "sdk_dll_sha256")),
                "collector_source_hash_field_present": any(k in d for k in ("collector_sha256", "source_sha256", "code_sha256")),
                "started_at": d.get("started_at"), "ended_at": d.get("ended_at"),
                "status": d.get("status"), "callback_errors": d.get("event_counts", {}).get("callback_errors"),
            })
        if source["role"] in {"ticks", "books", "context_ticks", "context_books"}:
            print("EVIDENCE", source["day"], source["role"], flush=True)
            scans.append(scan_stream(source["path"], day=source["day"], role=source["role"], expected_sha256=source["expected_sha256"]))
    sources["raw_schema_scan"] = {"method": "ALL_FROZEN_CANONICAL_STREAMS", "strategy_executed": False, "stream_count": len(scans), "precheck_path": str(local_paths["round2_precheck"]), "precheck_sha256": sha256(local_paths["round2_precheck"])}
    sources["source_manifests"] = {
        "count": len(source_manifests),
        "day_count": len({x["day"] for x in source_manifests}),
        "sdk_version_field_present_count": sum(bool(x["sdk_version_field_present"]) for x in source_manifests),
        "collector_source_hash_field_present_count": sum(bool(x["collector_source_hash_field_present"]) for x in source_manifests),
        "historical_source_binary_binding": "UNKNOWN",
    }
    sources["fee_inventory"] = {
        "scope": "CURRENT_PROJECT_MODULES_AND_EXPLICIT_EXISTING_LEDGER_INVENTORY_NOT_WHOLE_DISK",
        "current_checkout_live_runtime_directory_exists": (root / "stock-strategy/yuanta_live_runtime_v01/runtime").exists(),
        "current_checkout_broker_runtime_directory_exists": (root / "stock-strategy/yuanta_broker_execution_v01/runtime").exists(),
        "actual_broker_statement_found_in_this_scope": False,
        "external_checkout_private_runtime_opened": False,
        "account_db_queried": False, "credential_files_opened": False,
        "broker_login": False, "actual_account_fee": "UNKNOWN",
        "limitation": "Existing inventory locates external runtime but not authorized as a source binary/fee substitute. No absence claim about every location on disk.",
    }
    by_day = []
    for day in sorted(days):
        ss = [x for x in scans if x["day"] == day]
        ticks = [x for x in ss if x["role"].endswith("ticks")]
        books = [x for x in ss if x["role"].endswith("books")]
        fields = lambda rs, f: sum(x["field_present_counts"].get(f, 0) for x in rs)
        by_day.append({
            "day": day, "streams": len(ss), "tick_rows": sum(x["record_count"] for x in ticks),
            "book_rows": sum(x["record_count"] for x in books),
            "tick_quote_time_present": fields(ticks, "quote_time"),
            "tick_explicit_exchange_timestamp_present": fields(ticks, "exchange_time"),
            "book_quote_time_present": fields(books, "quote_time"),
            "book_explicit_exchange_timestamp_present": fields(books, "exchange_time"),
            "tick_ingest_sequence_present": fields(ticks, "ingest_sequence"),
            "book_ingest_sequence_present": fields(books, "ingest_sequence"),
            "callback_received_at_present": fields(ss, "callback_received_at"),
            "explicit_write_timestamp_present": sum(fields(ss, f) for f in ("written_at", "write_time", "persisted_at")),
            "raw_unit_metadata_present": sum(fields(ss, f) for f in ("raw_volume_unit", "volume_unit")),
            "ordinary_native_volume_decreases": sum(x["native_volume_message_changes"].get("down", 0) for x in ticks),
            "serial_gap_count_not_outage_assertion": sum(x["tick_serial_steps_per_symbol_original_file_order"].get("gap", 0) for x in ticks),
            "unit_status": "UNKNOWN", "actual_fee_status": "UNKNOWN", "strict_executable": "NA",
        })
    result = {
        "schema_version": SCHEMA, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "SHADOW_ONLY", "NO_LIVE_ORDER": True, "actual_orders": 0,
        "broker_connections": 0, "market_subscriptions": 0, "strategy_backtest_runs": 0,
        "evidence_audit": build_evidence_rows(sources), "sources": sources,
        "raw_stream_schema_audit": scans, "source_manifest_schema_audit": source_manifests,
        "evidence_daily_coverage": by_day,
        "correction_decision": {
            "model_parameter_changes": [], "data_corrected_version_created": False,
            "reason": "No new evidence changes a certified model input. Statutory 0.0015 condition matches the old setting; unit, actual account fees, latency and historical clocks remain unknown. Re-running identical performance adds no evidence.",
            "strict_executable": "NA", "all_affected_groups_must_rerun_if_future_change": True,
        },
        "provenance_schema_examples": [
            quantity_provenance("12", source_field="DealVol", semantic="EVENT_VOLUME_UNVERIFIED_SINGLE_FILL"),
            quantity_provenance("12", source_field="BuyVol1", semantic="DISPLAYED_DEPTH_SNAPSHOT", multiplier=1000, basis="ROUND1_ASSUMED_LOTS_SCENARIO_NOT_OFFICIAL_UNIT"),
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "evidence_tables.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    write_report(result, output)
    print("EVIDENCE COMPLETE", len(scans), "streams", sum(x["record_count"] for x in scans), "records", "actual_orders=0", flush=True)
    return result


def write_report(result, output):
    total_ticks = sum(x["tick_rows"] for x in result["evidence_daily_coverage"])
    total_books = sum(x["book_rows"] for x in result["evidence_daily_coverage"])
    day_count = len({x["day"] for x in result["evidence_daily_coverage"]})
    scans = result.get("raw_stream_schema_audit", [])
    stream_count = len(scans)
    hash_matches = sum(x.get("source_hash_matches_round2_precheck") is True for x in scans)
    sdk_description = sdk_evidence_description(result["sources"], result.get("source_manifest_schema_audit"))
    lines = ["# EVIDENCE_AUDIT：單位、費用與時間證據補強", "",
             "SHADOW_ONLY／NO_LIVE_ORDER／actual_orders=0。只讀既有封存，未跑策略、未登入、未新增訂閱，第一、二輪未覆寫。", "",
             f"本輪逐行檢查{day_count}日、{stream_count}個凍結原始串流：ticks {total_ticks:,}筆，五檔 {total_books:,}筆；逐檔SHA與第二輪precheck相符{hash_matches}/{stream_count}個。未輸出帳號、完整broker payload或官方範例登入資料。", "",
             "## 結論", "",
             "原始量到collector、normalizer、execution的轉換鏈與SDK版本證據分開處理。" + sdk_description + "DealVol與五檔買/賣量單位不因SDK識別而升級認證。", "",
             "原廠分時明細文件與全量資料不支持整日單調累積量假說，但仍不足證明每callback是一筆獨立撮合；五檔是各價檔顯示深度，不是成交量，也不是保證可成交量。", "",
             "實際帳戶佣金、最低費粒度、取整/退佣、券差費仍UNKNOWN。0.000855/最低20是原設定；官網支持牌告及最低費，不代表該帳戶實際折扣、退佣或取整方式。財政部來源可確認合格同日股票當沖在該期間1.5‰法定情境，但資格及券商實際代徵未認證，數值與原設定相同。", "",
             "歷史五檔無交易所時鐘，無法從receipt補回。接收時間是本機callback處理時刻，不是寫檔時間；SerialNo只對單一股票tick有意義。合成file-order不是跨通道交易所順序。callback空窗不能單獨斷言斷線；行情傳輸差也不是下單延遲。", "",
             "沒有足以改變模型輸入的新增確證，因此不建立假資料修正版、不重跑相同績效。STRICT_EXECUTABLE仍NA；本輪增加的是可追查證據與明確未知邊界，不是可交易可信度認證。", "",
             "## 原值轉換鏈", "",
             "1. SDK StockTickResult.DealVol / IndexFlag50或51的BuyVol1..5、SellVol1..5。",
             "2. collector_main以_safe_text=str保存deal_volume/buy_volumes/sell_volumes，未乘1000；received_at由utc_now寫入。",
             "3. collector.AppendOnlyRun.append同步JSONL寫入；未另封存每事件write timestamp，flush時機不等於行情發生時刻。",
             "4. data._events以float解析量，payload保留原str；volume_units明標UNKNOWN_SDK_NATIVE。五檔缺exchange time保留None。",
             "5. execution才依情境depth×multiplier(1000或1)×10%後向下取1000股整張容量；此為條件假設，並非單位證明。",
             "6. 新metadata schema保留raw_value/raw_unit/semantic、converted_value、multiplier、basis/SHA。未知時converted_value=null，假設情境另列scenario_value；拒收已轉換dict作原值，避免重複乘1000。", "",
             "## 逐項證據", ""]
    for row in result["evidence_audit"]:
        lines.extend([f"### {row['item_id']} — {row['confirmation_level']}", "",
                      f"原假設：{row['existing_assumption']}", "",
                      f"證據：{row['actual_evidence']}", "",
                      f"影響：{row['affected_scope']}", "",
                      f"修正或仍需：{row['correction_or_next_evidence']}", "",
                      "來源ID：" + ", ".join(row["source_ids"]), ""])
    lines.extend(["## 逐日欄位實數", "",
                  "以下是原始key存在且非null的計數，並不把空字串或時鐘來源認證混為一談。explicit write timestamp檢查written_at/write_time/persisted_at；callback及ingest欄位缺失不補造。", "",
                  "|日期|tick筆數|book筆數|book交易所時間欄|ingest序號(tick/book)|callback另存|write欄|量單位欄|", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for d in result["evidence_daily_coverage"]:
        lines.append(f"|{d['day']}|{d['tick_rows']}|{d['book_rows']}|{d['book_explicit_exchange_timestamp_present']}/{d['book_quote_time_present']}|{d['tick_ingest_sequence_present']}/{d['book_ingest_sequence_present']}|{d['callback_received_at_present']}|{d['explicit_write_timestamp_present']}|{d['raw_unit_metadata_present']}|")
    lines.extend(["", "## 來源版本、日期與SHA", "",
                  "SDK本機文件日期、官網release標記與是否可綁定DLL，以sources逐項metadata為準；本次抓取時間只證明稽核時取得，不回填為歷史當時可得時間。檔案mtime亦不當作文件製作或事件時間。", ""])
    for key, src in result["sources"].items():
        if "path" in src:
            lines.extend([f"- `{key}`：`{src['path']}`；SHA-256 `{src.get('sha256')}`。"])
        elif "url" in src:
            lines.extend([f"- `{key}`：[官方來源]({src['url']})；擷取 `{src.get('retrieved_at_utc', 'UNKNOWN')}`；SHA-256 `{src.get('content_sha256')}`；`{src['status']}`。"])
    if result.get("named_external_fee_ledger_audit"):
        a = result["named_external_fee_ledger_audit"]
        lines.extend(["", "## 具名專案帳戶庫之限定只讀費用查核", "",
                      f"依舊LEDGER_INVENTORY具名路徑，再由目前LaunchAgent確認WorkingDirectory為本專案、runtime-dir相同：`{a['scope_binding_status']}`。外部checkout HEAD僅作來源版本metadata，不混入其策略/參數/績效。", "",
                      "SQLite以mode=ro、query_only、讀取transaction讀取包含既有WAL之內容；沒有使用immutable忽略WAL後宣稱完整。只擷取schema、筆數與fees/fee/tax/commission/OCharge/OTax等allowlist鍵存在數；未輸出帳號、委託編號、成交價量、任意payload或私有部位。", ""])
        agg = a.get("fee_ledger_aggregate", {})
        lines.extend([f"資料庫：`{agg.get('path', 'UNKNOWN')}`。來源db/WAL SHA及查核前後一致性記於JSON；`persistent_database_and_wal_bytes_unchanged_during_read={agg.get('persistent_database_and_wal_bytes_unchanged_during_read', 'UNKNOWN')}`。", "",
                      f"紀錄數（不是本研究新增委託，也不自行認證皆為實單）：`{json.dumps(agg.get('record_counts', {}), ensure_ascii=False)}`。", "",
                      f"費用欄查核結論：`{a['actual_account_fee']}`。live_fills schema沒有費/稅欄；存在成交紀錄不等於存在可核對實收費率。", ""])
    lines.extend(["", "完整逐檔欄位計數、原量非敏感例子、來源版本/SHA、修正決策與metadata範例：`evidence_tables.json`。未讀credentials/Keychain/.env，未把paper推算費用當券商帳務。", ""])
    (Path(output) / "EVIDENCE_AUDIT.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--vendor-dir", type=Path)
    parser.add_argument("--fetch-official", action="store_true")
    parser.add_argument("--augment-fee-existing", action="store_true", help="Extend only the new follow-up audit with its one named, project-bound read-only fee ledger")
    parser.add_argument("--fee-launchagent", type=Path,
                        help="Explicit private LaunchAgent plist path; required for --augment-fee-existing")
    args = parser.parse_args(argv)
    if args.augment_fee_existing:
        if args.fee_launchagent is None:
            parser.error("--augment-fee-existing requires --fee-launchagent PATH")
        augment_existing_fee_evidence(args.root, args.output,
                                     fee_launchagent=args.fee_launchagent)
    else:
        run(args.root, args.output, vendor_dir=args.vendor_dir, fetch=args.fetch_official)


if __name__ == "__main__":
    main()
