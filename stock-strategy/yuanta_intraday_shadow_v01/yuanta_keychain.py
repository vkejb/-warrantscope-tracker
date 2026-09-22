"""Yuanta credential storage backed only by the user's macOS Keychain."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .main import _dragged_path, _normalise_account


SECURITY = Path("/usr/bin/security")
KEYCHAIN_ACCOUNT = "WarrantScope"
SERVICES = {
    "pfx": "com.warrantscope.yuanta.pfx-path.v1",
    "pfx_password": "com.warrantscope.yuanta.pfx-password.v1",
    "account": "com.warrantscope.yuanta.account.v1",
    "trading_password": "com.warrantscope.yuanta.trading-password.v1",
}
BINDING_SERVICE = "com.warrantscope.yuanta.credential-binding.v1"


def _read(service: str, *, run=subprocess.run) -> str | None:
    if not SECURITY.is_file():
        return None
    try:
        result = run([str(SECURITY), "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.rstrip("\n") if result.returncode == 0 and result.stdout.rstrip("\n") else None


def _binding(values: dict[str, str]) -> str:
    payload = "\0".join(values[key] for key in sorted(SERVICES))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_credentials(*, run=subprocess.run) -> dict[str, str]:
    values = {key: _read(service, run=run) for key, service in SERVICES.items()}
    binding = _read(BINDING_SERVICE, run=run)
    if any(value is None for value in values.values()) or binding != _binding(values):  # type: ignore[arg-type]
        raise RuntimeError("Yuanta Keychain credentials are absent, incomplete, or binding-mismatched")
    result = {key: str(value) for key, value in values.items()}
    pfx = _dragged_path(result["pfx"])
    if not pfx.is_file() or pfx.suffix.lower() != ".pfx":
        raise RuntimeError("Keychain PFX path is invalid")
    result["pfx"] = str(pfx)
    result["account"] = _normalise_account(result["account"])
    return result


def status() -> dict:
    try:
        values = load_credentials()
        return {"status": "KEYCHAIN_CONFIGURED", "pfx_exists": Path(values["pfx"]).is_file(), "account_format_valid": True, "secrets_printed": False}
    except Exception as exc:
        return {"status": "KEYCHAIN_NOT_READY", "reason": type(exc).__name__, "secrets_printed": False}


def _prompt_store(label: str, service: str, *, run=subprocess.run) -> bool:
    print(f"\n{label}", flush=True)
    print("請在下方隱藏輸入提示輸入；內容不會顯示，也不會寫入 repository。", flush=True)
    try:
        result = run([str(SECURITY), "add-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-U", "-w"], timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def configure_interactive(*, run=subprocess.run) -> dict:
    if not sys.stdin.isatty():
        return {"status": "INTERACTIVE_TERMINAL_REQUIRED"}
    if not SECURITY.is_file():
        return {"status": "MACOS_SECURITY_CLI_UNAVAILABLE"}
    prompts = (
        ("1/4 請輸入完整 PFX 憑證路徑（可先在 Finder 複製路徑）", SERVICES["pfx"]),
        ("2/4 請輸入 PFX 憑證密碼", SERVICES["pfx_password"]),
        ("3/4 請輸入元大證券帳號（不是 CMA 帳號）", SERVICES["account"]),
        ("4/4 請輸入證券電子交易密碼", SERVICES["trading_password"]),
    )
    for label, service in prompts:
        if not _prompt_store(label, service, run=run):
            return {"status": "KEYCHAIN_WRITE_FAILED", "failed_service": service}
    raw = {key: _read(service, run=run) for key, service in SERVICES.items()}
    if any(value is None for value in raw.values()):
        return {"status": "KEYCHAIN_READBACK_FAILED"}
    values = {key: str(value) for key, value in raw.items()}
    try:
        pfx = _dragged_path(values["pfx"])
        if not pfx.is_file() or pfx.suffix.lower() != ".pfx":
            raise ValueError("invalid PFX")
        _normalise_account(values["account"])
    except ValueError:
        return {"status": "PFX_OR_ACCOUNT_VALIDATION_FAILED"}
    digest = _binding(values)
    saved = run([str(SECURITY), "add-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", BINDING_SERVICE, "-U", "-w", digest], capture_output=True, timeout=30, check=False)
    if saved.returncode != 0:
        return {"status": "KEYCHAIN_BINDING_WRITE_FAILED"}
    return status()


def main(argv=None) -> int:
    command = (argv or sys.argv[1:] or ["status"])[0]
    result = configure_interactive() if command == "configure" else status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "KEYCHAIN_CONFIGURED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
