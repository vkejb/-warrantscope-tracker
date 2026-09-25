from __future__ import annotations

from pathlib import Path
import subprocess

SECURITY = Path("/usr/bin/security")
ACCOUNT = "WarrantScopeTrading"
TOKEN_SERVICE = "com.warrantscope.telegram.trading.bot-token.v1"
CHAT_SERVICE = "com.warrantscope.telegram.trading.chat-id.v1"


class TradingBotKeychainError(RuntimeError):
    pass


def _read(service: str) -> str:
    if not SECURITY.is_file():
        raise TradingBotKeychainError("macOS security CLI is unavailable")
    result = subprocess.run(
        [
            str(SECURITY),
            "find-generic-password",
            "-a", ACCOUNT,
            "-s", service,
            "-w",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=8,
    )
    if result.returncode != 0:
        raise TradingBotKeychainError(f"Trading Bot Keychain item missing: {service}")
    value = result.stdout.strip()
    if not value:
        raise TradingBotKeychainError(f"Trading Bot Keychain item empty: {service}")
    return value


def load_trading_bot_credentials() -> tuple[str, str]:
    token = _read(TOKEN_SERVICE)
    chat_id = _read(CHAT_SERVICE)
    if not chat_id.lstrip("-").isdigit():
        raise TradingBotKeychainError("Trading Bot chat id has invalid format")
    return token, chat_id
