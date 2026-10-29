"""Offline historical restriction audit; optional public exchange evidence fetch.

Only public HTTPS exchange reports can be fetched, and only with the explicit
--acquire-public-evidence switch. No SDK, strategy, broker or scheduler import.
The default command reuses byte-preserved receipts; it never calls a network.
No positive exchange observation certifies historical point-in-time receipt or
the investor's broker entitlement. Raw quote streams are never opened here.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
import urllib.request

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent
STUDY = MODULE.parent
ROOT = STUDY.parent
FLAG = "暫停現股賣出後現款買進當沖註記"
UNKNOWN = "UNKNOWN"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def roc_day(value):
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        try:
            return datetime.strptime(text, "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            return None
    if re.fullmatch(r"\d{7}", text):
        return roc_day(f"{int(text[:3])+1911:04d}{text[3:]}")
    match = re.fullmatch(r"(\d{2,4})/(\d{1,2})/(\d{1,2})", text)
    if not match:
        return None
    y, m, d = map(int, match.groups())
    y = y+1911 if y < 1911 else y
    try:
        return datetime(y, m, d).strftime("%Y%m%d")
    except ValueError:
        return None


def interval(value):
    dates = re.findall(r"\d{2,4}/\d{1,2}/\d{1,2}", str(value))
    return tuple(roc_day(x) for x in dates[:2]) if len(dates) >= 2 else (None, None)


def interval_contains(day, start, end):
    return bool(start and end and start <= day <= end)


def decoded_receipt(receipt):
    raw = base64.b64decode(receipt["http_body_base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != receipt["http_body_sha256"]:
        raise ValueError("PUBLIC_RECEIPT_HASH_MISMATCH")
    return json.loads(raw)


def source_rows(doc):
    if isinstance(doc, list):
        return doc
    tables = doc.get("tables", [doc])
    return [{str(k): v for k, v in zip(table.get("fields", []), values)}
            for table in tables for values in table.get("data", [])]


def fetch_receipt(source_id, url):
    if not url.startswith(("https://www.twse.com.tw/", "https://www.tpex.org.tw/")):
        raise ValueError("ONLY_OFFICIAL_EXCHANGE_HTTPS_ALLOWED")
    observed = datetime.now(timezone.utc).isoformat()
    try:
        with urllib.request.urlopen(url, timeout=25) as response:
            body = response.read()
            result = {"source_id": source_id, "url": url, "obtained_at": observed,
                      "http_status": response.status,
                      "content_type": response.headers.get("Content-Type"),
                      "http_body_sha256": hashlib.sha256(body).hexdigest(),
                      "http_body_bytes": len(body),
                      "http_body_base64": base64.b64encode(body).decode("ascii")}
        json.loads(body)
        return result
    except Exception as exc:
        return {"source_id": source_id, "url": url, "obtained_at": observed,
                "status": "UNAVAILABLE", "error_type": type(exc).__name__}


def acquire(days):
    end = max(days)
    requests = [("TWSE_DAY_"+day,
                 f"https://www.twse.com.tw/exchangeReport/TWTB4U?date={day}&response=json")
                for day in days]
    requests += [
        ("TWSE_DISPOSAL", "https://www.twse.com.tw/announcement/punish?response=json"
         f"&startDate=20260101&endDate={end}"),
        ("TWSE_HALT", "https://www.twse.com.tw/rwd/zh/afterTrading/TWTAWU?response=json"
         f"&startDate=20260101&endDate={end}"),
        ("TPEX_SELL_FIRST_SUSPENSION", "https://www.tpex.org.tw/www/zh-tw/intraday/n13his"
         f"?startDate=2026/01/01&endDate={end[:4]}/{end[4:6]}/{end[6:]}&response=json"),
        ("TPEX_DISPOSAL", "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
         f"?startDate=2026/01/01&endDate={end[:4]}/{end[4:6]}/{end[6:]}&response=json"),
        ("TPEX_HALT", "https://www.tpex.org.tw/www/zh-tw/bulletin/sprcHis?date=2026&cate=1&response=json"),
    ]
    return [fetch_receipt(source_id, url) for source_id, url in requests]


def validated_sources(receipts, days):
    """Fail closed on silently ignored date arguments (notably TPEx APIs)."""
    end = max(days)
    valid, inventory = {}, []
    for receipt in receipts:
        source_id = receipt["source_id"]
        row = {k: v for k, v in receipt.items() if k != "http_body_base64"}
        row.update(available_to_original_system_before_entry=UNKNOWN,
                   exchange_publication_time=UNKNOWN,
                   current_retrieval_is_historical_PIT_proof=False)
        try:
            doc = decoded_receipt(receipt)
            if source_id.startswith("TWSE_DAY_"):
                okay = doc.get("date") == source_id[-8:] and doc.get("stat") == "OK"
                okay = okay and any(FLAG in x.get("fields", []) for x in doc.get("tables", []))
            elif source_id.startswith("TPEX_DISPOSAL") or source_id == "TPEX_SELL_FIRST_SUSPENSION":
                query = parse_qs(urlparse(receipt["url"]).query)
                start_arg = roc_day(query.get("startDate", [None])[0])
                end_arg = roc_day(query.get("endDate", [None])[0])
                okay = bool(start_arg and end_arg and end_arg == end)
                okay = okay and doc.get("date") == start_arg+"~"+end_arg and doc.get("stat", "").lower() == "ok"
            elif source_id == "TPEX_HALT":
                okay = doc.get("date") == "2026" and doc.get("stat", "").lower() == "ok"
            else:
                title = doc.get("title", "")
                okay = "115/01/01" in title and f"115/{end[4:6]}/{end[6:]}" in title
                okay = okay and (doc.get("stat") == "OK" or "沒有符合條件" in doc.get("stat", ""))
            row["requested_period_verified"] = bool(okay)
            row["reported_status"] = doc.get("stat")
            row["reported_date_or_title"] = doc.get("date", doc.get("title"))
            row["reported_row_count"] = len(source_rows(doc))
            if okay:
                valid[source_id] = doc
        except (KeyError, ValueError, TypeError):
            row["requested_period_verified"] = False
        inventory.append(row)
    return valid, inventory


def restrictions(valid):
    events = []
    for source_id in valid:
        if not (source_id in {"TWSE_DISPOSAL", "TPEX_SELL_FIRST_SUSPENSION", "TPEX_HALT", "TWSE_HALT"}
                or source_id.startswith("TPEX_DISPOSAL")):
            continue
        for ordinal, r in enumerate(source_rows(valid.get(source_id, {})), 1):
            symbol = str(r.get("證券代號", r.get("股票代號", r.get("有價證券代號", "")))).strip()
            if not symbol:
                continue
            if source_id == "TWSE_DISPOSAL" or source_id.startswith("TPEX_DISPOSAL"):
                start, end = interval(r.get("處置起迄時間", r.get("處置起訖時間")))
                event_type = "DISPOSAL"
            elif source_id == "TPEX_SELL_FIRST_SUSPENSION":
                start, end = roc_day(r.get("停止先賣後買開始日")), roc_day(r.get("停止先賣後買結束日"))
                event_type = "SELL_FIRST_SUSPENSION"
            else:
                start, end = roc_day(r.get("暫停交易日期")), roc_day(r.get("恢復交易日期"))
                event_type = "HALT_OR_RESUMPTION"
            events.append({"restriction_event_id": f"{source_id}:{ordinal}", "source_id": source_id,
                           "symbol": symbol, "event_type": event_type, "effective_start": start,
                           "effective_end": end, "publication_date": roc_day(r.get("公布日期")),
                           "suspension_time": r.get("暫停交易時間"), "resumption_time": r.get("恢復交易時間"),
                           "source_record": r, "available_to_original_system_at_entry": UNKNOWN})
    return events


def event_matches(day, event):
    if event["event_type"] == "HALT_OR_RESUMPTION":
        start, end = event["effective_start"], event["effective_end"]
        # Day-level overlap is not a claim that a resume-day entry was halted.
        return bool(start and start <= day and (end is None or day <= end))
    return interval_contains(day, event["effective_start"], event["effective_end"])


def discover_metadata(study):
    """Only small manifest/session JSONs and file stats, never quote bodies."""
    base = Path(study)/"yuanta_intraday_shadow_v01/runtime"
    result = []
    for path in sorted((base/"runs").glob("*/run_manifest.json")):
        m = load(path)
        session = base/"session_analyses"/path.parent.name/"session_manifest.json"
        s = load(session) if session.is_file() else {}
        result.append({"run_id": path.parent.name, "day": str(m.get("started_at", ""))[:10].replace("-", ""),
                       "started_at": m.get("started_at"), "ended_at": m.get("ended_at"),
                       "manifest_path": str(path), "manifest_sha256": digest(path),
                       "manifest_status": m.get("status"), "coverage_status": s.get("coverage_status", "UNRECORDED"),
                       "stream_coverage_pass": s.get("stream_coverage_pass"),
                       "callback_errors": m.get("event_counts", {}).get("callback_errors"),
                       "session_manifest_path": str(session) if session.is_file() else None,
                       "session_manifest_sha256": digest(session) if session.is_file() else None,
                       "recorded_max_market_event_gap_seconds": s.get("max_market_event_gap_seconds"),
                       "stream_stats_only": {name: (path.parent/name).stat().st_size
                                             for name in m.get("artifacts", {})
                                             if name in {"ticks.jsonl.gz", "books.jsonl.gz", "market_context_ticks.jsonl.gz", "market_context_books.jsonl.gz"}
                                             and (path.parent/name).is_file()},
                       "quote_bodies_opened": False,
                       "callback_silence_proves_disconnect": False})
    return result


def prospective_status(metadata, old_days, frozen_at):
    newer = [r for r in metadata if r["day"] > max(old_days)]
    # Even a complete new archive is only a candidate until the research owner
    # attests results unseen and independently verifies its source coverage.
    def after_freeze(value):
        if not frozen_at or not value:
            return False
        try:
            start = datetime.fromisoformat(value.replace("Z", "+00:00"))
            freeze = datetime.fromisoformat(frozen_at.replace("Z", "+00:00"))
            return bool(start.tzinfo and freeze.tzinfo and start > freeze)
        except (ValueError, TypeError):
            return False
    candidates = [r for r in newer if after_freeze(r.get("started_at"))
                  and r.get("manifest_status") == "COMPLETE" and r.get("stream_coverage_pass") is True]
    return {"status": "PENDING_NO_NEW_DATE_ARCHIVE" if not newer else "PENDING_UNSEEN_AND_FREEZE_ATTESTATION",
            "old_known_dates": old_days, "last_archive_day": max((r["day"] for r in metadata), default=None),
            "newer_archive_count": len(newer), "metadata_candidate_count": len(candidates),
            "prospective_approved_count": 0, "freeze_time_used": frozen_at,
            "unseen_result_attestation": "PENDING", "new_schedules_started": 0,
            "new_market_connections": 0, "signal_or_performance_on_unseen_dates_read": False}


def impact_checks(table_sets, new_rejects):
    checks = []
    for version, named_tables in table_sets.items():
        for table_name, rows in named_tables.items():
            groups = {}
            for r in rows:
                default_phase = "PAIRED_EXIT" if table_name == "paired_exits" else ("ROUND1" if version == "ROUND1" else "UNSPECIFIED")
                key = (r.get("phase", default_phase), r.get("entry_model", r.get("side", "UNSPECIFIED")))
                groups.setdefault(key, []).append(r)
            for (phase, model), group in sorted(groups.items()):
                affected = [r for r in group if (r.get("day"), str(r.get("symbol"))) in new_rejects]
                checks.append({"version": version, "table": table_name, "phase": phase,
                               "entry_model_or_side": model, "row_count": len(group),
                               "independent_signal_count": len({r.get("signal_id") for r in group if r.get("signal_id")}),
                               "affected_row_count": len(affected),
                               "affected_signal_ids": sorted({r["signal_id"] for r in affected if r.get("signal_id")}),
                               "affected_experiments_or_variants": sorted({r.get("experiment", r.get("variant", "UNSPECIFIED")) for r in affected}),
                               "match_basis": "EXACT_SYMBOL_AND_EVENT_EFFECTIVE_SESSION_DAY_NOT_TODAYS_LIST"})
    return checks


def compare_cache_response(cache, receipt):
    previous = load(cache["path"])
    current = decoded_receipt(receipt)
    old = source_rows(previous["raw_response"])
    new = source_rows(current)
    before = {str(r["證券代號"]): r for r in old if "證券代號" in r}
    after = {str(r["證券代號"]): r for r in new if "證券代號" in r}
    differences = [{"symbol": symbol, "field": field,
                    "previous": before[symbol].get(field), "current": after[symbol].get(field)}
                   for symbol in sorted(before.keys() & after.keys())
                   for field in sorted(set(before[symbol]) | set(after[symbol]))
                   if before[symbol].get(field) != after[symbol].get(field)]
    flag_changed = [r for r in differences if r["field"] == FLAG]
    return {"day": cache["day"], "legacy_cache_path": cache["path"],
            "legacy_cache_file_hash_verified": digest(cache["path"]) == cache["sha256"],
            "legacy_recorded_original_http_sha256": previous.get("sha256"),
            "current_http_sha256": receipt["http_body_sha256"],
            "recorded_original_http_hash_reobserved": previous.get("sha256") == receipt["http_body_sha256"],
            "parsed_response_unchanged": previous["raw_response"] == current,
            "added_symbols": sorted(after.keys()-before.keys()), "removed_symbols": sorted(before.keys()-after.keys()),
            "field_differences": differences, "suspension_flag_differences": flag_changed,
            "historical_PIT_receipt_proved": False}


def build(receipts, *, comparison_frozen_at=None):
    r1 = MODULE/"results_round1_v2"
    tables, manifest = load(r1/"tables.json"), load(r1/"run_manifest.json")
    days = manifest["conditional_days"]
    valid, inventory = validated_sources(receipts, days)
    events = restrictions(valid)
    old_cache = {r["day"]: r for r in load(r1/"eligibility_input_freeze.json")["sources"]}
    receipts_by_id = {r["source_id"]: r for r in receipts if r["source_id"] in valid}
    cache_comparisons = [compare_cache_response(cache, receipts_by_id["TWSE_DAY_"+day])
                         for day, cache in old_cache.items() if "TWSE_DAY_"+day in receipts_by_id]
    original = {(r["day"], str(r["symbol"])): r for r in tables["eligibility_audit"]}
    candidates, daily, matched = [], [], []
    for day in days:
        a = next(r for r in tables["DATA_AUDIT"] if r["day"] == day)
        watchpath = STUDY/"yuanta_intraday_shadow_v01/runtime/runs"/a["canonical_run_id"]/"watchlist.json"
        stocks = load(watchpath)["stocks"]
        source_id = "TWSE_DAY_"+day
        day_flags = {str(r["證券代號"]): r.get(FLAG) for r in source_rows(valid.get(source_id, {})) if "證券代號" in r}
        dayrows = []
        for stock in stocks:
            symbol = str(stock["stock_id"])
            matching = [e for e in events if e["symbol"] == symbol and event_matches(day, e)]
            definite = [e for e in matching if e["event_type"] != "HALT_OR_RESUMPTION"]
            flag = day_flags.get(symbol) if stock.get("market") == "TWSE" else None
            old = original[(day, symbol)]
            negative = flag == "Y" or bool(definite)
            row = {"day": day, "symbol": symbol, "market": stock.get("market", UNKNOWN),
                   "rank": stock.get("rank"), "legacy_status": old["status"],
                   "legacy_historical_flag": old.get("historical_flag"),
                   "current_observed_TWSE_flag": flag,
                   "retrospective_negative_restriction": bool(negative),
                   "retrospective_restriction_status": "KNOWN_RESTRICTION_REJECT" if negative else UNKNOWN,
                   "halt_day_overlap_requires_entry_time_check": any(e["event_type"] == "HALT_OR_RESUMPTION" for e in matching),
                   "matched_event_ids": [e["restriction_event_id"] for e in matching],
                   "new_rejection_vs_original": bool(negative and old["status"] != "KNOWN_SUSPENSION_EXCLUDED"),
                   "historical_positive_eligibility": "EXCHANGE_DAILY_LISTED_BLANK_FLAG_ONLY" if flag == "" else UNKNOWN,
                   "available_to_original_system_before_entry": UNKNOWN,
                   "broker_account_entitlement": UNKNOWN, "strict_executable": "NA",
                   "daily_source_id": source_id if source_id in valid else None,
                   "watchlist_path": str(watchpath), "watchlist_sha256": digest(watchpath)}
            dayrows.append(row)
            for event in matching:
                matched.append({"day": day, "symbol": symbol, **event})
        candidates += dayrows
        cache = old_cache.get(day)
        daily.append({"day": day, "canonical_run_id": a["canonical_run_id"], "candidate_count": len(dayrows),
                      "TWSE_candidates": sum(x["market"] == "TWSE" for x in dayrows),
                      "TPEX_candidates": sum(x["market"] != "TWSE" for x in dayrows),
                      "legacy_cache_path": cache["path"] if cache else None,
                      "legacy_cache_expected_sha256": cache["sha256"] if cache else None,
                      "legacy_cache_hash_verified": digest(cache["path"]) == cache["sha256"] if cache else None,
                      "daily_exchange_report_retrieved": source_id in valid,
                      "blank_TWSE_flags": sum(x["current_observed_TWSE_flag"] == "" for x in dayrows),
                      "Y_TWSE_flags": sum(x["current_observed_TWSE_flag"] == "Y" for x in dayrows),
                      "known_retrospective_restriction_count": sum(x["retrospective_negative_restriction"] for x in dayrows),
                      "new_rejection_vs_original_count": sum(x["new_rejection_vs_original"] for x in dayrows),
                      "entry_time_PIT_confirmed_count": 0, "broker_entitlement_confirmed_count": 0,
                      "strict_executable": "NA", "disconnection_status": UNKNOWN,
                      "gap_interpretation": "RECEIPT_GAP_NOT_PROOF_OF_OUTAGE_OR_HALT"})
    signals = load(MODULE/"round2/results_round2_v1/signals_enriched.json")
    all_signals = signals if isinstance(signals, list) else list(signals.values())
    new_rejects = {(r["day"], r["symbol"]) for r in candidates if r["new_rejection_vs_original"]}
    affected = [{k: s.get(k) for k in ("day", "symbol", "signal_id", "time")}
                for s in all_signals if (s["day"], str(s["symbol"])) in new_rejects]
    r2tables = load(MODULE/"round2/results_round2_v1/tables.json")
    impacts = impact_checks({"ROUND1": {k: tables[k] for k in ("signals", "trades")},
                             "ROUND2": {k: r2tables[k] for k in ("signals", "trades", "portfolio_dispositions", "paired_exits")}}, new_rejects)
    metadata = discover_metadata(STUDY)
    return {"audit_id": "HISTORICAL_ELIGIBILITY_EVIDENCE_FOLLOWUP_V01",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "SHADOW_ONLY": True, "NO_LIVE_ORDER": True, "actual_orders": 0,
            "raw_quote_streams_read": 0,
            "source_provenance": {"round1_tables_sha256": digest(r1/"tables.json"),
                                  "round1_eligibility_freeze_sha256": digest(r1/"eligibility_input_freeze.json"),
                                  "round2_tables_sha256": digest(MODULE/"round2/results_round2_v1/tables.json"),
                                  "round2_enriched_signals_sha256": digest(MODULE/"round2/results_round2_v1/signals_enriched.json"),
                                  "historical_cache_fetcher_path": str(STUDY/"short_side_study_v01/fetch_eligibility.py"),
                                  "historical_cache_fetcher_sha256": digest(STUDY/"short_side_study_v01/fetch_eligibility.py")},
            "tables": {"eligibility_daily": daily, "eligibility_candidates": candidates,
                       "matched_restriction_events": matched, "public_evidence_inventory": inventory,
                       "archive_manifest_inventory": metadata, "restriction_impact_checks": impacts,
                       "legacy_vs_current_response": cache_comparisons},
            "public_http_receipts": receipts,
            "new_negative_evidence_affects_S1_signal_ids": affected,
            "data_revision_review_required": any(r["affected_row_count"] for r in impacts),
            "restriction_data_revision": {
                "label": "RETROSPECTIVE_NEGATIVE_RESTRICTION_MASK_V1_NOT_PIT_RECONSTRUCTION",
                "new_negative_candidate_stock_days": len(new_rejects),
                "all_observed_round1_round2_candidate_and_trade_intersections": sum(r["affected_row_count"] for r in impacts),
                "same_performance_replay_required": any(r["affected_row_count"] for r in impacts),
                "reason": "All observed original candidate, disposition, fixed-entry and account-entry rows were joined by symbol and effective session day; zero intersection means no recorded input selection changes, not certified eligibility.",
                "no_unknown_promoted_to_legal": True},
            "new_qualifying_date_validation": prospective_status(metadata, days, comparison_frozen_at),
            "limits": ["Current retrospective exchange reports do not attest historical receipt or announcement time.",
                       "Blank daily suspension flag is not positive broker-account entitlement.",
                       "Disposal effective windows are retrospective date evidence; dynamic adjustments and publication receipt remain unproven.",
                       "An incomplete public response cannot certify coverage; a shorter-period response does not certify full-year negative completeness.",
                       "No halt record in a specific endpoint is not a global no-halt certificate.",
                       "Candidate callback silence is not a verified outage; no raw stream was opened in this metadata audit."]}


def write_report(result, path):
    rows = result["tables"]["eligibility_daily"]
    lines = ["# 歷史資格與新日期證據補強", "", "SHADOW_ONLY／NO_LIVE_ORDER／actual_orders=0；未讀取新日期訊號或績效。", "",
             "本次新取得的交易所回應為『目前查到的歷史事件』，不是當時系統已收到的證據。所有日期的券商帳戶當沖資格仍 UNKNOWN，STRICT_EXECUTABLE 仍 NA。", "",
             "| 日期 | 候選 | 舊 TWSE 快取 | 目前歷史 Y | 明確限制 | 相較原模型新增排除 |", "|---|---:|---|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['day']} | {r['candidate_count']} | {'有，hash 已核對' if r['legacy_cache_hash_verified'] else '無'} | {r['Y_TWSE_flags']} | {r['known_retrospective_restriction_count']} | {r['new_rejection_vs_original_count']} |")
    lines += ["", "## 確認與尚未確認", "",
              "- 原快取 fetch_eligibility.py 將 TWSE 表內空白註記映為 True，非空白映為 False；原 HTTP bytes 未保存、沒有取得時間。freeze 只證明後來保存檔的內容未變，不證明當時已可用。",
              "- 本次 HTTP 原始 bytes 以 base64 保存於 eligibility_tables.json，逐筆附 URL、取得時間、SHA-256、回應日期及請求日期核對。網頁介面/API 規格不保證查詢參數生效，程式對回應期間不符 fail closed。",
              "- 新查得的處置生效起迄與暫停先賣後買期間，可用於獨立『回溯資料修正版』的負面排除；公告日不等於實際公告時刻，也不證明當時 collector 已收到。不能把這些回應回填成 point-in-time 原始決策。",
              "- 歷史五檔交易所時鐘缺失不可補回；callback 間隔只代表沒有收到事件，不能單憑該間隔說斷線、無成交或停牌。",
              ""]
    comparisons = result["tables"].get("legacy_vs_current_response", [])
    matched = [r["day"] for r in comparisons if r["recorded_original_http_hash_reobserved"]]
    changed = [r for r in comparisons if not r["recorded_original_http_hash_reobserved"]]
    lines += [f"原歷史快取重查 {len(comparisons)} 日；記載 HTTP SHA 與本次 bytes 相同 {len(matched)} 日（{'、'.join(matched) or '無'}），不同 {len(changed)} 日。", ""]
    for comparison in changed:
        lines.append(f"- {comparison['day']}：新增證券 {len(comparison['added_symbols'])}、移除 {len(comparison['removed_symbols'])}、暫停旗標差異 {len(comparison['suspension_flag_differences'])}。")
        for difference in comparison["field_differences"]:
            lines.append(f"  - {difference['symbol']}／{difference['field']}：{difference['previous']} → {difference['current']}。")
    lines += ["", "差異明細在 legacy_vs_current_response；歷史頁面的 date 欄及目前重新取得的相同 bytes，都不能獨立證明當時取得時間。", ""]
    for source in result["tables"]["public_evidence_inventory"]:
        if not source.get("requested_period_verified"):
            lines.append(f"- 來源 {source['source_id']}：未確認，錯誤 {source.get('error_type', 'UNKNOWN')}；保留失敗紀錄，不視為無限制。")
    lines += ["", "若使用較短期間的完整回應，只能支持該回應聲明的期間，不宣稱全年限制已完整排除。", "",
              "## 新增限制對既有訊號的影響", "",
              f"影響已知 S1 signal_id 數：{len(result['new_negative_evidence_affects_S1_signal_ids'])}。需資料修正版審查：{result['data_revision_review_required']}。不得只重跑 R2/R5；若採用限制，應所有受影響舊比較組共同重播並與原結果分開。", ""]
    for r in result["new_negative_evidence_affects_S1_signal_ids"]:
        lines.append(f"- {r['day']} {r['symbol']}：`{r['signal_id']}`")
    lines += ["", "交叉核對不只 S1：第一輪 signals/trades 的全部模型（含 LONG、M0、S1、S2）及第二輪 ENTRY／ACCOUNT_EXIT／FIXED_EXIT 的訊號、交易、委託處置及配對出場均逐股日檢查。完整明細在 restriction_impact_checks。空集合僅表示現存紀錄與『已查得』新增限制沒有交集，不表示未知限制不存在。", "",
              "| 版本／表 | 階段／模型 | 行數 | 受新增限制行數 |", "|---|---|---:|---:|"]
    for check in result["tables"]["restriction_impact_checks"]:
        lines.append(f"| {check['version']}／{check['table']} | {check['phase']}／{check['entry_model_or_side']} | {check['row_count']} | {check['affected_row_count']} |")
    p = result["new_qualifying_date_validation"]
    lines += ["", "## 前瞻驗證", "", f"目前最後封存日期 {p['last_archive_day']}；超過原 13 日範圍的新日期封存 {p['newer_archive_count']} 個。狀態：{p['status']}。", "",
              "僅掃 run_manifest/session_manifest 與行情檔案大小，沒有解壓行情、讀取尚未見過日期的訊號或損益。新版凍結後的未見新交易日尚待資料；不建立排程、不連券商。", "",
              "## 證據路徑及官方來源", "", "- 本份逐日／逐股／事件／來源與封存清冊：`eligibility_tables.json`", "- 原資格 freeze：`cash_short_intraday_research_v01/results_round1_v2/eligibility_input_freeze.json`", "- 原回應 cache：`short_side_study_v01/sources/*_twse_eligibility.json`", ""]
    for r in result["tables"]["public_evidence_inventory"]:
        lines.append(f"- `{r['source_id']}`：{r['url']}；取得 {r['obtained_at']}；SHA `{r.get('http_body_sha256', 'UNKNOWN')}`；期間驗證 {r['requested_period_verified']}。")
    Path(path).write_text("\n".join(lines)+"\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE/"results_followup_v1/eligibility_tables.json")
    parser.add_argument("--receipts-from", type=Path)
    parser.add_argument("--comparison-frozen-at")
    parser.add_argument("--acquire-public-evidence", action="store_true")
    args = parser.parse_args(argv)
    if args.output.exists():
        raise SystemExit("REFUSE_TO_OVERWRITE_EXISTING_EVIDENCE")
    days = load(MODULE/"results_round1_v2/run_manifest.json")["conditional_days"]
    if args.acquire_public_evidence:
        receipts = acquire(days)
    elif args.receipts_from:
        receipts = load(args.receipts_from)["public_http_receipts"]
    else:
        receipts = []
    result = build(receipts, comparison_frozen_at=args.comparison_frozen_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    write_report(result, args.output.parent/"HISTORICAL_ELIGIBILITY.md")
    print(json.dumps({"output": str(args.output), "new_negative_S1_signal_count": len(result["new_negative_evidence_affects_S1_signal_ids"]),
                      "prospective": result["new_qualifying_date_validation"]["status"], "actual_orders": 0}))


if __name__ == "__main__":
    main()
