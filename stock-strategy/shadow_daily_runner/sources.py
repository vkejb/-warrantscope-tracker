from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode

from .config import CFG, RunnerConfig
from .io_utils import sha256_bytes, utc_timestamp, write_immutable


USER_AGENT = "WarrantScope-ShadowDailyRunner/0.1 official-EOD-only"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source: str
    request_url: str
    retrieved_at_utc: str
    sha256: str
    path: Path
    payload: bytes

    def metadata(self) -> dict:
        return {
            "source": self.source,
            "request_url": self.request_url,
            "retrieved_at_utc": self.retrieved_at_utc,
            "sha256": self.sha256,
            "bytes": len(self.payload),
            "path": str(self.path),
        }


class OfficialSourceClient:
    """Small curl-based client with immutable, content-addressed raw snapshots."""

    def __init__(self, cfg: RunnerConfig = CFG) -> None:
        self.cfg = cfg
        self._last_network_request = 0.0

    @staticmethod
    def _payload_matches(payload: bytes, expected_kind: str | None) -> bool:
        if expected_kind is None:
            return bool(payload)
        if expected_kind == "json":
            return payload.lstrip().startswith((b"{", b"["))
        if expected_kind == "zip":
            return payload.startswith(b"PK")
        if expected_kind == "tpex_csv":
            for encoding in ("big5", "cp950", "utf-8-sig"):
                try:
                    # The fixed byte probe can end between the two bytes of a
                    # Big5 character.  Ignoring only that truncated tail is
                    # safe here; the full parser still performs strict decode.
                    text = payload[:4096].decode(encoding, errors="ignore")
                except UnicodeDecodeError:
                    continue
                if "上櫃股票每日收盤行情" in text and "資料日期:" in text:
                    return True
            return False
        raise AssertionError(expected_kind)

    def _get(
        self,
        source: str,
        url: str,
        params: dict[str, str] | None = None,
        *,
        suffix: str | None = None,
        refresh: bool = True,
        expected_kind: str | None = None,
    ) -> SourceSnapshot:
        query = urlencode(params or {})
        request_url = f"{url}?{query}" if query else url
        source_dir = self.cfg.raw_dir / source
        if not refresh and source_dir.is_dir():
            candidates = sorted(
                path
                for path in source_dir.iterdir()
                if path.is_file() and not path.name.endswith(".metadata.json")
            )
            valid_candidates = [
                path
                for path in candidates
                if self._payload_matches(path.read_bytes(), expected_kind)
            ]
            if valid_candidates:
                path = max(valid_candidates, key=lambda item: item.stat().st_mtime_ns)
                payload = path.read_bytes()
                digest = sha256_bytes(payload)
                if path.stem != digest:
                    raise RuntimeError(f"cached source hash/name mismatch: {path}")
                metadata_path = source_dir / f"{digest}.metadata.json"
                if metadata_path.is_file():
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                else:
                    retrieved = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    ).isoformat(timespec="seconds").replace("+00:00", "Z")
                    metadata = {
                        "source": source,
                        "request_url": request_url,
                        "retrieved_at_utc": retrieved,
                        "sha256": digest,
                        "bytes": len(payload),
                        "local_cache_import": True,
                    }
                    write_immutable(
                        metadata_path,
                        (
                            json.dumps(
                                metadata,
                                ensure_ascii=False,
                                sort_keys=True,
                                indent=2,
                            )
                            + "\n"
                        ).encode("utf-8"),
                    )
                return SourceSnapshot(
                    source=source,
                    request_url=request_url,
                    retrieved_at_utc=str(metadata.get("retrieved_at_utc", "CACHED")),
                    sha256=digest,
                    path=path,
                    payload=payload,
                )
        payload = b""
        last_error = ""
        for attempt in range(4):
            since_previous = time.monotonic() - self._last_network_request
            polite_interval = 1.75
            if since_previous < polite_interval:
                time.sleep(polite_interval - since_previous)
            command = [
                "/usr/bin/curl",
                "-fsSL",
                "--max-time",
                "90",
                "--retry",
                "2",
                "--retry-delay",
                "2",
                "-A",
                USER_AGENT,
                request_url,
            ]
            completed = subprocess.run(command, capture_output=True, check=False)
            self._last_network_request = time.monotonic()
            if completed.returncode:
                last_error = completed.stderr.decode("utf-8", errors="replace").strip()
            elif not completed.stdout:
                last_error = "empty response"
            elif not self._payload_matches(completed.stdout, expected_kind):
                # TWSE sometimes returns an HTTP-200 security HTML page under
                # request pressure.  An HTML page can never enter the
                # reusable cache or an official-input manifest.
                last_error = f"unexpected content for {expected_kind or 'source'}"
            else:
                payload = completed.stdout
                break
            if attempt < 3:
                time.sleep(5.0 * (attempt + 1))
        if not payload:
            raise RuntimeError(f"download failed for {source}: {last_error}")
        retrieved_at_utc = utc_timestamp()
        digest = sha256_bytes(payload)
        extension = suffix or (
            ".json" if payload.lstrip().startswith((b"{", b"[")) else ".bin"
        )
        path = self.cfg.raw_dir / source / f"{digest}{extension}"
        write_immutable(path, payload)
        metadata_path = self.cfg.raw_dir / source / f"{digest}.metadata.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if (
                metadata.get("sha256") != digest
                or metadata.get("request_url") != request_url
                or int(metadata.get("bytes", -1)) != len(payload)
            ):
                raise RuntimeError(f"cached source metadata conflict: {metadata_path}")
        else:
            metadata = {
                "source": source,
                "request_url": request_url,
                "retrieved_at_utc": retrieved_at_utc,
                "sha256": digest,
                "bytes": len(payload),
            }
            metadata_payload = (
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
            write_immutable(metadata_path, metadata_payload)
        return SourceSnapshot(
            source=source,
            request_url=request_url,
            # This is the retrieval used by this run.  The content-addressed
            # sidecar above intentionally keeps the first-seen timestamp.
            retrieved_at_utc=retrieved_at_utc,
            sha256=digest,
            path=path,
            payload=payload,
        )

    def release_week(self, year: int, week: int) -> SourceSnapshot:
        name = f"weekly_{year}_W{week:02d}.zip"
        return self._get(
            f"release_{year}_W{week:02d}",
            f"{self.cfg.release_base_url}/{name}",
            suffix=".zip",
            refresh=False,
            expected_kind="zip",
        )

    def twse_eod(self, day: str, *, refresh: bool = True) -> SourceSnapshot:
        return self._get(
            f"twse_eod_{day}",
            self.cfg.twse_eod_url,
            {"date": day, "type": "ALLBUT0999", "response": "json"},
            refresh=refresh,
            expected_kind="json",
        )

    def tpex_eod(self, day: str, *, refresh: bool = True) -> SourceSnapshot:
        roc_year = int(day[:4]) - 1911
        slash = f"{roc_year:03d}/{day[4:6]}/{day[6:]}"
        return self._get(
            f"tpex_eod_{day}",
            self.cfg.tpex_eod_url,
            {
                "date": slash,
                "type": "AL",
                "id": "",
                "response": "csv",
                "order": "0",
                "sort": "asc",
            },
            suffix=".csv",
            refresh=refresh,
            expected_kind="tpex_csv",
        )

    def calendar(self) -> SourceSnapshot:
        return self._get(
            "twse_calendar", self.cfg.twse_calendar_url, expected_kind="json"
        )


def decode_json(snapshot: SourceSnapshot) -> object:
    try:
        return json.loads(snapshot.payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON from {snapshot.source}") from exc
