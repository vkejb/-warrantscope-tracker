from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlencode


STUDY_ID = "OFFICIAL_SOURCE_TRANSPORT_DIAGNOSTIC"
PUBLIC_USER_AGENT = "WarrantScopeResearch/1.0 (official-data transport diagnostic)"
PUBLIC_HEADERS = (
    ("Accept", "application/json,text/csv,text/plain,*/*"),
    ("Accept-Language", "zh-TW,zh;q=0.9,en;q=0.8"),
)
HISTORICAL_DATES = (20200102, 20210104, 20220103, 20240529, 20250102)
ALLOWED_STATUS = {
    "WORKING_OFFICIAL",
    "WORKING_WITH_PUBLIC_SESSION",
    "REDIRECT_BLOCKED",
    "RATE_LIMITED",
    "UNUSABLE",
}


@dataclass(frozen=True, slots=True)
class Probe:
    trace_id: str
    endpoint_id: str
    source: str
    market: str
    family: str
    route_type: str
    url: str
    params: tuple[tuple[str, str], ...]
    response_format: str
    expected_date: int | None
    supports_historical_date: bool
    client_profile: str = "DESCRIPTIVE_PUBLIC_CLIENT_NO_COOKIE"
    follow_redirects: bool = True
    referer: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _redact_header(name: str, value: str) -> str:
    if name.lower() in {"set-cookie", "cookie", "authorization", "proxy-authorization"}:
        cookie_name = value.split("=", 1)[0].strip() if "=" in value else "redacted"
        return f"<redacted:{cookie_name};sha256={_sha256(value.encode())}>"
    return value


def _parse_header_chains(raw: str) -> list[dict]:
    chains = []
    current = None
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.startswith("HTTP/"):
            if current is not None:
                chains.append(current)
            parts = line.split(None, 2)
            current = {
                "status_line": line,
                "status": int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
                "headers": {},
            }
        elif current is not None and ":" in line:
            name, value = line.split(":", 1)
            clean = _redact_header(name.strip(), value.strip())
            existing = current["headers"].get(name.lower())
            current["headers"][name.lower()] = clean if existing is None else [existing, clean]
    if current is not None:
        chains.append(current)
    return chains


def _decode_text(body: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _validate_body(probe: Probe, body: bytes, content_type: str) -> dict:
    result = {
        "valid_market_payload": False,
        "payload_kind": "UNKNOWN",
        "reported_date": None,
        "row_count": None,
        "schema_fields": [],
        "validation_error": None,
    }
    text = _decode_text(body)
    if probe.response_format == "csv":
        rows = list(csv.reader(text.splitlines()))
        nonempty = [row for row in rows if row]
        header_index = next((
            index for index, row in enumerate(nonempty[:20])
            if row and row[0].strip() in {"證券代號", "股票代號", "代號"}
        ), None)
        data_rows = [] if header_index is None else [
            row for row in nonempty[header_index + 1:]
            if row and row[0].strip().removeprefix('="').removesuffix('"').isdigit()
        ]
        result.update({
            "payload_kind": "CSV",
            "row_count": len(data_rows),
            "schema_fields": nonempty[header_index][:24] if header_index is not None else [],
            "valid_market_payload": header_index is not None and bool(data_rows),
        })
        if not result["valid_market_payload"]:
            result["validation_error"] = "CSV schema/rows not recognized"
        return result
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        result.update({
            "payload_kind": "HTML_OR_TEXT" if "html" in text[:500].lower() else "NON_JSON_TEXT",
            "validation_error": "response is not JSON",
            "body_preview": text[:500],
        })
        return result
    if isinstance(payload, list):
        result.update({
            "payload_kind": "JSON_ARRAY",
            "row_count": len(payload),
            "schema_fields": sorted(payload[0])[:40] if payload and isinstance(payload[0], dict) else [],
            "valid_market_payload": bool(payload),
        })
        if not payload:
            result["validation_error"] = "empty JSON array"
        return result
    if not isinstance(payload, dict):
        result.update({"payload_kind": "JSON_OTHER", "validation_error": "JSON root is not object/array"})
        return result
    reported_date = payload.get("date")
    try:
        reported_date = int(reported_date) if reported_date is not None else None
    except (TypeError, ValueError):
        reported_date = None
    tables = payload.get("tables", [])
    if probe.source == "TWSE_T86_RWD":
        rows = payload.get("data", [])
        fields = payload.get("fields", [])
        okay_status = payload.get("stat") == "OK"
    elif probe.source == "TWSE_MI_MARGN_RWD":
        detail = next((table for table in tables if "融資融券彙總" in table.get("title", "")), None)
        rows = [] if detail is None else detail.get("data", [])
        fields = [] if detail is None else detail.get("fields", [])
        okay_status = payload.get("stat") == "OK"
    else:
        first = tables[0] if tables else {}
        rows = first.get("data", [])
        fields = first.get("fields", [])
        okay_status = str(payload.get("stat", "")).lower() == "ok"
    date_ok = probe.expected_date is None or reported_date == probe.expected_date
    result.update({
        "payload_kind": "JSON_OBJECT",
        "reported_date": reported_date,
        "row_count": len(rows),
        "schema_fields": fields[:40],
        "valid_market_payload": bool(okay_status and date_ok and rows),
    })
    if not result["valid_market_payload"]:
        result["validation_error"] = f"status/date/rows failed: stat={payload.get('stat')} date={reported_date} rows={len(rows)}"
    return result


def _classify(trace: dict, public_session: bool = False) -> str:
    status = int(trace.get("http_code") or 0)
    preview = str(trace.get("body_validation", {}).get("body_preview", "")).lower()
    if status in {403, 429, 503} or "anti-ddos" in preview or "access denied" in preview:
        return "RATE_LIMITED"
    if 300 <= status < 400:
        return "REDIRECT_BLOCKED"
    if status == 200 and trace["body_validation"]["valid_market_payload"]:
        return "WORKING_WITH_PUBLIC_SESSION" if public_session else "WORKING_OFFICIAL"
    return "UNUSABLE"


def _run_probe(probe: Probe, temporary: Path) -> dict:
    header_path = temporary / f"{probe.trace_id}.headers"
    body_path = temporary / f"{probe.trace_id}.body"
    command = [
        "/usr/bin/curl", "-sS", "--compressed", "--max-time", "45", "--max-redirs", "10",
        "-D", str(header_path), "-o", str(body_path),
        "-w", '{"http_code":%{http_code},"url_effective":"%{url_effective}","num_redirects":%{num_redirects},"content_type":"%{content_type}","size_download":%{size_download},"time_total":%{time_total},"ssl_verify_result":%{ssl_verify_result},"http_version":"%{http_version}"}',
    ]
    request_headers = []
    if probe.follow_redirects:
        command.append("-L")
    if probe.client_profile != "CURL_DEFAULT_NO_COOKIE":
        command.extend(["-A", PUBLIC_USER_AGENT])
        request_headers.append(["User-Agent", PUBLIC_USER_AGENT])
        for name, value in PUBLIC_HEADERS:
            command.extend(["-H", f"{name}: {value}"])
            request_headers.append([name, value])
    if probe.referer:
        command.extend(["-e", probe.referer])
        request_headers.append(["Referer", probe.referer])
    command.extend(["--get", probe.url])
    for name, value in probe.params:
        command.extend(["--data-urlencode", f"{name}={value}"])
    started = _utc_now()
    completed = subprocess.run(command, text=True, capture_output=True, timeout=55)
    finished = _utc_now()
    body = body_path.read_bytes() if body_path.exists() else b""
    raw_headers = header_path.read_text(encoding="iso-8859-1") if header_path.exists() else ""
    try:
        metrics = json.loads(completed.stdout) if completed.stdout else {}
    except json.JSONDecodeError:
        metrics = {"write_out_parse_error": completed.stdout[:500]}
    content_type = str(metrics.get("content_type") or "")
    validation = _validate_body(probe, body, content_type)
    trace = {
        "study_id": STUDY_ID,
        "probe": asdict(probe),
        "requested_url": f"{probe.url}?{urlencode(dict(probe.params))}" if probe.params else probe.url,
        "request_headers": request_headers,
        "cookie_session_used": False,
        "started_at_utc": started,
        "finished_at_utc": finished,
        "curl_exit_code": completed.returncode,
        "curl_error": completed.stderr.strip() or None,
        **metrics,
        "response_chain": _parse_header_chains(raw_headers),
        "response_length": len(body),
        "body_sha256": _sha256(body),
        "body_validation": validation,
    }
    trace["classification"] = _classify(trace)
    if trace["classification"] not in ALLOWED_STATUS:
        raise AssertionError("invalid transport classification")
    return trace


def _historical_probes() -> list[Probe]:
    probes = []
    for date in HISTORICAL_DATES:
        text = str(date)
        roc = f"{int(text[:4]) - 1911:03d}/{text[4:6]}/{text[6:]}"
        iso = f"{text[:4]}/{text[4:6]}/{text[6:]}"
        probes.extend((
            Probe(f"twse_t86_{date}", "TWSE_RWD_T86_JSON", "TWSE_T86_RWD", "TWSE", "INSTITUTIONAL", "RWD_HISTORICAL", "https://www.twse.com.tw/rwd/zh/fund/T86", (("response", "json"), ("date", text), ("selectType", "ALLBUT0999")), "json", date, True),
            Probe(f"twse_margin_{date}", "TWSE_RWD_MI_MARGN_JSON", "TWSE_MI_MARGN_RWD", "TWSE", "MARGIN_SHORT", "RWD_HISTORICAL", "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN", (("response", "json"), ("date", text), ("selectType", "ALL")), "json", date, True),
            Probe(f"tpex_inst_{date}", "TPEX_LEGACY_INSTITUTIONAL_JSON", "TPEX_INSTITUTIONAL", "TPEX", "INSTITUTIONAL", "LEGACY_HISTORICAL", "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php", (("l", "zh-tw"), ("o", "json"), ("se", "EW"), ("t", "D"), ("d", roc), ("s", "0,asc")), "json", date, True),
            Probe(f"tpex_margin_{date}", "TPEX_MARGIN_BALANCE_JSON", "TPEX_MARGIN_BALANCE", "TPEX", "MARGIN_SHORT", "HISTORICAL", "https://www.tpex.org.tw/www/zh-tw/margin/balance", (("date", iso), ("id", ""), ("response", "json")), "json", date, True),
        ))
    return probes


def _comparison_probes() -> list[Probe]:
    return [
        Probe("twse_t86_default_curl_20200102", "TWSE_RWD_T86_DEFAULT_CURL", "TWSE_T86_RWD", "TWSE", "INSTITUTIONAL", "RWD_HISTORICAL", "https://www.twse.com.tw/rwd/zh/fund/T86", (("response", "json"), ("date", "20200102"), ("selectType", "ALLBUT0999")), "json", 20200102, True, client_profile="CURL_DEFAULT_NO_COOKIE"),
        Probe("twse_t86_csv_20200102", "TWSE_RWD_T86_CSV", "TWSE_T86_RWD", "TWSE", "INSTITUTIONAL", "RWD_HISTORICAL_CSV", "https://www.twse.com.tw/rwd/zh/fund/T86", (("response", "csv"), ("date", "20200102"), ("selectType", "ALLBUT0999")), "csv", 20200102, True),
        Probe("twse_margin_csv_20200102", "TWSE_RWD_MI_MARGN_CSV", "TWSE_MI_MARGN_RWD", "TWSE", "MARGIN_SHORT", "RWD_HISTORICAL_CSV", "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN", (("response", "csv"), ("date", "20200102"), ("selectType", "ALL")), "csv", 20200102, True),
        Probe("twse_legacy_t86_20200102", "TWSE_LEGACY_T86_JSON", "TWSE_T86_RWD", "TWSE", "INSTITUTIONAL", "LEGACY_HISTORICAL", "https://www.twse.com.tw/fund/T86", (("response", "json"), ("date", "20200102"), ("selectType", "ALLBUT0999")), "json", 20200102, True),
        Probe("twse_legacy_margin_20200102", "TWSE_LEGACY_MI_MARGN_JSON", "TWSE_MI_MARGN_RWD", "TWSE", "MARGIN_SHORT", "LEGACY_HISTORICAL", "https://www.twse.com.tw/exchangeReport/MI_MARGN", (("response", "json"), ("date", "20200102"), ("selectType", "ALL")), "json", 20200102, True),
        Probe("twse_openapi_margin_current", "TWSE_OPENAPI_MI_MARGN_CURRENT", "TWSE_OPENAPI_MI_MARGN", "TWSE", "MARGIN_SHORT", "OPENAPI_CURRENT_ONLY", "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN", (), "json", None, False),
        Probe("tpex_openapi_inst_current", "TPEX_OPENAPI_INSTITUTIONAL_CURRENT", "TPEX_OPENAPI_INSTITUTIONAL", "TPEX", "INSTITUTIONAL", "OPENAPI_CURRENT_ONLY", "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading", (), "json", None, False),
        Probe("tpex_openapi_margin_current", "TPEX_OPENAPI_MARGIN_CURRENT", "TPEX_OPENAPI_MARGIN", "TPEX", "MARGIN_SHORT", "OPENAPI_CURRENT_ONLY", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance", (), "json", None, False),
    ]


def _endpoint_rows(traces: list[dict]) -> list[dict]:
    grouped = {}
    for trace in traces:
        probe = trace["probe"]
        grouped.setdefault(probe["endpoint_id"], []).append(trace)
    rows = []
    for endpoint_id, items in sorted(grouped.items()):
        first = items[0]["probe"]
        working = sum(item["classification"] in {"WORKING_OFFICIAL", "WORKING_WITH_PUBLIC_SESSION"} for item in items)
        classifications = {item["classification"] for item in items}
        status = next(iter(classifications)) if len(classifications) == 1 else ("WORKING_OFFICIAL" if working == len(items) else "UNUSABLE")
        rows.append({
            "endpoint_id": endpoint_id,
            "source": first["source"],
            "market": first["market"],
            "feature_family": first["family"],
            "route_type": first["route_type"],
            "base_url": first["url"],
            "response_format": first["response_format"],
            "supports_historical_date": first["supports_historical_date"],
            "client_profile": first["client_profile"],
            "follow_redirects": first["follow_redirects"],
            "cookie_session_required": False,
            "dates_tested": "|".join(str(item["probe"]["expected_date"] or "CURRENT") for item in items),
            "sample_count": len(items),
            "success_count": working,
            "failure_count": len(items) - working,
            "status": status,
            "historical_research_usable": bool(first["supports_historical_date"] and working == len(items)),
            "notes": "No cookie/session used. TLS verification enabled; compression and redirect following enabled.",
        })
    rows.append({
        "endpoint_id": "TWSE_OPENAPI_T86_NOT_DOCUMENTED",
        "source": "TWSE_OPENAPI",
        "market": "TWSE",
        "feature_family": "INSTITUTIONAL",
        "route_type": "OPENAPI",
        "base_url": "https://openapi.twse.com.tw/v1/swagger.json",
        "response_format": "json",
        "supports_historical_date": False,
        "client_profile": "DOCUMENTATION_AUDIT",
        "follow_redirects": True,
        "cookie_session_required": False,
        "dates_tested": "NONE",
        "sample_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "status": "UNUSABLE",
        "historical_research_usable": False,
        "notes": "Official Swagger has no T86 endpoint; no request was fabricated.",
    })
    if not all(row["status"] in ALLOWED_STATUS for row in rows):
        raise AssertionError("endpoint matrix contains invalid status")
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def run(output_dir: Path, interval_seconds: float) -> dict:
    tracked = (output_dir / "transport_diagnostic.json", output_dir / "source_endpoint_matrix.csv", output_dir / "http_trace_samples")
    if any(path.exists() for path in tracked):
        raise FileExistsError("refusing to overwrite published transport diagnostic")
    started = _utc_now()
    probes = _comparison_probes() + _historical_probes()
    staging = Path(tempfile.mkdtemp(prefix=".transport_diagnostic_", dir=output_dir))
    temporary = staging / "runtime"
    traces_dir = staging / "http_trace_samples"
    temporary.mkdir()
    traces_dir.mkdir()
    traces = []
    try:
        for index, probe in enumerate(probes):
            trace = _run_probe(probe, temporary)
            traces.append(trace)
            (traces_dir / f"{probe.trace_id}.json").write_text(
                json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({
                "probe": probe.trace_id,
                "classification": trace["classification"],
                "http_code": trace.get("http_code"),
                "valid_market_payload": trace["body_validation"]["valid_market_payload"],
            }, ensure_ascii=False), flush=True)
            if interval_seconds and index + 1 < len(probes):
                time.sleep(interval_seconds)
        rows = _endpoint_rows(traces)
        _write_csv(staging / "source_endpoint_matrix.csv", rows)
        cross_year_ids = {
            "TWSE_RWD_T86_JSON", "TWSE_RWD_MI_MARGN_JSON",
            "TPEX_LEGACY_INSTITUTIONAL_JSON", "TPEX_MARGIN_BALANCE_JSON",
        }
        cross_year = [row for row in rows if row["endpoint_id"] in cross_year_ids]
        resolved = len(cross_year) == 4 and all(row["success_count"] == 5 and row["historical_research_usable"] for row in cross_year)
        default_trace = next(trace for trace in traces if trace["probe"]["endpoint_id"] == "TWSE_RWD_T86_DEFAULT_CURL")
        if default_trace["classification"] in {"WORKING_OFFICIAL", "WORKING_WITH_PUBLIC_SESSION"}:
            root_cause = (
                "The previously observed TWSE HiNetCDN HTTP 307 was transient: the identical public "
                "historical route also returned a valid HTTP 200 payload with curl's default User-Agent "
                "during this diagnostic. The evidence does not support a permanent redirect or a required "
                "cookie/User-Agent workaround. Use a descriptive public client profile, bounded retries, "
                "and resumable immutable caching; continue to fail closed on any future 307 response."
            )
            root_cause_confidence = "TRANSIENT_CDN_OR_EDGE_SECURITY_STATE_SUPPORTED; EXACT_TRIGGER_UNPROVEN"
        else:
            root_cause = (
                "The historical endpoints work with a descriptive public client profile, but the control "
                "request did not; a client-profile-sensitive CDN response is supported."
            ) if resolved else "No stable official cross-year route established."
            root_cause_confidence = "CLIENT_PROFILE_SENSITIVE" if resolved else "UNRESOLVED"
        summary = {
            "study_id": STUDY_ID,
            "status": "TRANSPORT_RESOLVED" if resolved else "OFFICIAL_TRANSPORT_UNRESOLVED",
            "started_at_utc": started,
            "finished_at_utc": _utc_now(),
            "root_cause": root_cause,
            "root_cause_confidence": root_cause_confidence,
            "selected_client_profile": {
                "user_agent": PUBLIC_USER_AGENT,
                "headers": dict(PUBLIC_HEADERS),
                "follow_redirects": True,
                "compressed_transfer": True,
                "cookie_session_required": False,
                "tls_verification": True,
            },
            "default_curl_result": {
                "classification": default_trace["classification"],
                "http_code": default_trace.get("http_code"),
                "location_chain": [item["headers"].get("location") for item in default_trace["response_chain"] if item["headers"].get("location")],
                "valid_market_payload": default_trace["body_validation"]["valid_market_payload"],
            },
            "cross_year_dates": list(HISTORICAL_DATES),
            "cross_year_required_endpoints": sorted(cross_year_ids),
            "cross_year_endpoint_results": cross_year,
            "twse_sample_success": all(row["success_count"] == 5 for row in cross_year if row["market"] == "TWSE"),
            "tpex_sample_success": all(row["success_count"] == 5 for row in cross_year if row["market"] == "TPEX"),
            "ready_to_update_downloader": resolved,
            "ready_to_resume_formal_acquisition": resolved,
            "formal_batch_started": False,
            "formal_model_run": False,
            "model_fit_count": 0,
            "pit_lag_sessions": 1,
            "cookie_values_persisted": False,
            "actual_orders": 0,
            "actual_fills": 0,
            "broker_connections": 0,
            "trace_count": len(traces),
            "trace_sha256": {
                path.name: _sha256(path.read_bytes()) for path in sorted(traces_dir.glob("*.json"))
            },
        }
        (staging / "transport_diagnostic.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        shutil.rmtree(temporary)
        (staging / "transport_diagnostic.json").replace(output_dir / "transport_diagnostic.json")
        (staging / "source_endpoint_matrix.csv").replace(output_dir / "source_endpoint_matrix.csv")
        traces_dir.replace(output_dir / "http_trace_samples")
        staging.rmdir()
        return summary
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=STUDY_ID)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interval-seconds", type=float, default=4.0)
    args = parser.parse_args(argv)
    if args.interval_seconds < 0:
        raise ValueError("interval must be nonnegative")
    summary = run(args.output_dir, args.interval_seconds)
    print(json.dumps({
        "status": summary["status"],
        "twse_sample_success": summary["twse_sample_success"],
        "tpex_sample_success": summary["tpex_sample_success"],
        "ready_to_resume_formal_acquisition": summary["ready_to_resume_formal_acquisition"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
