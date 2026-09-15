from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, patch

from prospective_notifications_v01.keychain import (
    CHAT_ENV, CHAT_SERVICE, PAIR_SERVICE, TOKEN_ENV, TOKEN_SERVICE,
    _pair_digest,
    configure_interactive, load_into_environment,
)
from prospective_notifications_v01.main import main as notification_main


class KeychainTests(unittest.TestCase):
    def test_complete_environment_never_reads_keychain(self):
        environment = {TOKEN_ENV: "private-token", CHAT_ENV: "1234"}
        run = Mock(side_effect=AssertionError("Keychain must not be read"))
        self.assertEqual(load_into_environment(environment=environment, run=run), "ENV_CONFIGURED")

    def test_keychain_loads_pair_without_printing_or_logging_secret(self):
        environment = {}
        def fake_run(command, **_kwargs):
            service = command[command.index("-s") + 1]
            values = {TOKEN_SERVICE: "private-token", CHAT_SERVICE: "12345", PAIR_SERVICE: _pair_digest("private-token", "12345")}
            return subprocess.CompletedProcess(command, 0, values[service] + "\n", "")
        self.assertEqual(load_into_environment(environment=environment, run=fake_run), "KEYCHAIN_CONFIGURED")
        self.assertEqual(environment[TOKEN_ENV], "private-token")
        self.assertEqual(environment[CHAT_ENV], "12345")

    def test_missing_item_keeps_pair_unconfigured(self):
        environment = {}
        run = Mock(return_value=subprocess.CompletedProcess([], 44, "", "not found"))
        self.assertEqual(load_into_environment(environment=environment, run=run), "KEYCHAIN_NOT_CONFIGURED")
        self.assertNotIn(TOKEN_ENV, environment)
        self.assertNotIn(CHAT_ENV, environment)

    def test_partial_reconfiguration_cannot_send_to_old_recipient(self):
        environment = {}
        values = {TOKEN_SERVICE: "new-token", CHAT_SERVICE: "111", PAIR_SERVICE: _pair_digest("old-token", "111")}
        def fake_run(command, **_kwargs):
            service = command[command.index("-s") + 1]
            return subprocess.CompletedProcess(command, 0, values[service] + "\n", "")
        self.assertEqual(load_into_environment(environment=environment, run=fake_run), "KEYCHAIN_NOT_CONFIGURED")
        self.assertNotIn(TOKEN_ENV, environment)

    def test_setup_requires_terminal(self):
        with patch("prospective_notifications_v01.keychain.sys.stdin.isatty", return_value=False):
            self.assertEqual(configure_interactive()["status"], "INTERACTIVE_TERMINAL_REQUIRED")

    def test_setup_uses_hidden_security_prompt_and_unique_private_chat(self):
        calls = []
        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        def fake_api(_token, method):
            if method == "getMe":
                return {"ok": True, "result": {"is_bot": True, "username": "ExampleBot"}}
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": ""}}
            return {"ok": True, "result": [{"message": {"chat": {"id": 12345, "type": "private"}}}]}
        with patch("prospective_notifications_v01.keychain.sys.stdin.isatty", return_value=True), patch("prospective_notifications_v01.keychain._read_item", side_effect=["private-token", "12345", _pair_digest("private-token", "12345")]), patch("prospective_notifications_v01.keychain._official_json", side_effect=fake_api):
            result = configure_interactive(run=fake_run)
        self.assertEqual(result["status"], "KEYCHAIN_CONFIGURED")
        self.assertNotIn("private-token", str(calls))
        self.assertNotIn("12345", str(calls))
        add_commands = [command for command in calls if "add-generic-password" in command]
        self.assertEqual(len(add_commands), 3)
        self.assertTrue(all(command[-1] == "-w" for command in add_commands[:2]))
        self.assertEqual(add_commands[2][-1], _pair_digest("private-token", "12345"))

    def test_multiple_chats_fail_closed_without_saving_recipient(self):
        calls = []
        def fake_run(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        def fake_api(_token, method):
            if method == "getMe":
                return {"ok": True, "result": {"is_bot": True, "username": "ExampleBot"}}
            if method == "getWebhookInfo":
                return {"ok": True, "result": {"url": ""}}
            return {"ok": True, "result": [{"message": {"chat": {"id": 1, "type": "private"}}}, {"message": {"chat": {"id": 2, "type": "private"}}}]}
        with patch("prospective_notifications_v01.keychain.sys.stdin.isatty", return_value=True), patch("prospective_notifications_v01.keychain._read_item", return_value="private-token"), patch("prospective_notifications_v01.keychain._official_json", side_effect=fake_api):
            result = configure_interactive(run=fake_run)
        self.assertEqual(result["status"], "MULTIPLE_PRIVATE_CHATS_FAIL_CLOSED")
        self.assertFalse(any(CHAT_SERVICE in command for command in calls))

    def test_test_command_fails_if_configured_telegram_delivery_fails(self):
        with patch("prospective_notifications_v01.main.load_into_environment", return_value="KEYCHAIN_CONFIGURED"), patch("prospective_notifications_v01.main.notify", return_value={"TELEGRAM": "FAILED", "MACOS_LOCAL_NOTIFICATION": "SUCCESS"}):
            self.assertEqual(notification_main(["test"]), 3)


if __name__ == "__main__":
    unittest.main()
