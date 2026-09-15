"""Private macOS Keychain adapter for launchd notification credentials.

The notifier still reads only environment variables.  This adapter never
prints, logs, or persists a token in repository files or launchd plists.
"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


SECURITY = Path("/usr/bin/security")
ACCOUNT = "WarrantScope"
TOKEN_SERVICE = "com.warrantscope.telegram.bot-token.v1"
CHAT_SERVICE = "com.warrantscope.telegram.chat-id.v1"
PAIR_SERVICE = "com.warrantscope.telegram.verified-pair.v1"
TOKEN_ENV = "WARRANTSCOPE_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "WARRANTSCOPE_TELEGRAM_CHAT_ID"


def _read_item(service: str, *, run=subprocess.run) -> str | None:
    if not SECURITY.is_file():
        return None
    try:
        result = run(
            [str(SECURITY), "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
            capture_output=True, text=True, check=False, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _pair_digest(token: str, chat: str) -> str:
    return hashlib.sha256(f"{token}\x00{chat}".encode("utf-8")).hexdigest()


def load_into_environment(*, environment: dict[str, str] | None = None, run=subprocess.run) -> str:
    """Best-effort pre-notification load. Keychain errors never block sealing."""

    target = os.environ if environment is None else environment
    if target.get(TOKEN_ENV) and target.get(CHAT_ENV):
        return "ENV_CONFIGURED"
    token = _read_item(TOKEN_SERVICE, run=run)
    chat = _read_item(CHAT_SERVICE, run=run)
    verified_pair = _read_item(PAIR_SERVICE, run=run)
    if not token or not chat or not chat.lstrip("-").isdigit() or verified_pair != _pair_digest(token, chat):
        return "KEYCHAIN_NOT_CONFIGURED"
    target[TOKEN_ENV] = token
    target[CHAT_ENV] = chat
    return "KEYCHAIN_CONFIGURED"


def _official_json(token: str, method: str) -> dict | None:
    # Token exists only in this process's memory and the authenticated HTTPS
    # request.  Never expose an exception URL, which contains the token.
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            value = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    return value if isinstance(value, dict) and value.get("ok") is True else None


def configure_interactive(*, run=subprocess.run) -> dict:
    """Prompt locally using `security -w` (last option), never token argv."""

    if not sys.stdin.isatty():
        return {"status": "INTERACTIVE_TERMINAL_REQUIRED"}
    if not SECURITY.is_file():
        return {"status": "KEYCHAIN_CLI_UNAVAILABLE"}
    print("請在下一個 Keychain 提示輸入 bot token；輸入不會顯示。不要貼到聊天或 repo。", flush=True)
    try:
        saved = run(
            [str(SECURITY), "add-generic-password", "-a", ACCOUNT,
             "-s", TOKEN_SERVICE, "-U", "-w"],
            check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "TOKEN_KEYCHAIN_SAVE_FAILED"}
    if saved.returncode != 0:
        return {"status": "TOKEN_KEYCHAIN_SAVE_FAILED"}
    token = _read_item(TOKEN_SERVICE, run=run)
    if not token:
        return {"status": "TOKEN_KEYCHAIN_READ_FAILED"}
    bot = _official_json(token, "getMe")
    if bot is None or not bot.get("result", {}).get("is_bot"):
        return {"status": "BOT_TOKEN_INVALID_OR_OFFICIAL_API_UNAVAILABLE"}
    username = str(bot["result"].get("username", ""))
    webhook = _official_json(token, "getWebhookInfo")
    if webhook is not None and webhook.get("result", {}).get("url"):
        return {"status": "WEBHOOK_ACTIVE_GETUPDATES_UNAVAILABLE", "bot_username": username}
    updates = _official_json(token, "getUpdates")
    if updates is None:
        return {"status": "OFFICIAL_UPDATES_UNAVAILABLE", "bot_username": username}
    private_chats = {
        str(chat["id"])
        for update in updates.get("result", [])
        for chat in [update.get("message", {}).get("chat", {})]
        if chat.get("type") == "private" and isinstance(chat.get("id"), int)
    }
    if not private_chats:
        return {"status": "NO_PRIVATE_CHAT_UPDATE_SEND_NEW_MESSAGE", "bot_username": username}
    if len(private_chats) != 1:
        return {"status": "MULTIPLE_PRIVATE_CHATS_FAIL_CLOSED", "bot_username": username, "private_chat_count": len(private_chats)}
    chat_id = next(iter(private_chats))
    print(f"已確認 bot：@{username}；偵測到唯一私人對話 ID：{chat_id}", flush=True)
    print("請在下一個 Keychain 提示輸入上方對話 ID。", flush=True)
    try:
        saved = run(
            [str(SECURITY), "add-generic-password", "-a", ACCOUNT,
             "-s", CHAT_SERVICE, "-U", "-w"],
            check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "CHAT_KEYCHAIN_SAVE_FAILED", "bot_username": username}
    if saved.returncode != 0:
        return {"status": "CHAT_KEYCHAIN_SAVE_FAILED", "bot_username": username}
    stored_chat = _read_item(CHAT_SERVICE, run=run)
    if stored_chat != chat_id:
        return {"status": "CHAT_KEYCHAIN_ID_MISMATCH", "bot_username": username}
    # This non-reversible verification marker binds the two separately stored
    # values. A partial setup or mistyped chat ID cannot pair a new token with
    # an old recipient at the next launchd run.
    pair_digest = _pair_digest(token, chat_id)
    try:
        saved = run(
            [str(SECURITY), "add-generic-password", "-a", ACCOUNT,
             "-s", PAIR_SERVICE, "-U", "-w", pair_digest],
            capture_output=True, text=True, check=False, timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "PAIR_VERIFICATION_SAVE_FAILED", "bot_username": username}
    if saved.returncode != 0 or _read_item(PAIR_SERVICE, run=run) != pair_digest:
        return {"status": "PAIR_VERIFICATION_SAVE_FAILED", "bot_username": username}
    return {"status": "KEYCHAIN_CONFIGURED", "bot_username": username, "chat_id_last4": chat_id[-4:]}
