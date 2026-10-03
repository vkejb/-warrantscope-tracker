"""Synthetic account singleton tests; no application locks or broker access."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
from unittest import TestCase

from yuanta_live_runtime_v01.account_lock import (
    acquire_account_lock, release_account_lock, account_lock_health,
)


class AccountLockTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="account-lock-test-")
        self.root = Path(self.temp.name) / "locks"
        self.locks = []

    def tearDown(self):
        for handle in self.locks:
            release_account_lock(handle)
        self.temp.cleanup()

    def acquire(self, account="S00000000000", environment="PROD", **kwargs):
        result = acquire_account_lock(account, environment, lock_root=self.root, **kwargs)
        self.locks.append(result)
        return result

    def test_same_account_cannot_run_from_two_runtime_directories(self):
        first = self.acquire(runtime_dir=Path(self.temp.name) / "A")
        with self.assertRaisesRegex(RuntimeError, "another controller"):
            self.acquire(runtime_dir=Path(self.temp.name) / "B")
        self.assertTrue(account_lock_health(first.name, os.getpid(), expected_instance=first.instance_id))

    def test_account_alias_and_case_have_same_lock(self):
        self.acquire()
        for alias in ("00000000000", "s0000-0000000", " S00000000000 "):
            with self.subTest(alias=alias), self.assertRaises(RuntimeError):
                self.acquire(alias)

    def test_different_account_or_environment_is_independent(self):
        first = self.acquire()
        other = self.acquire("S11111111111")
        uat = self.acquire(environment="UAT")
        self.assertEqual(len({first.name, other.name, uat.name}), 3)

    def test_release_preserves_inode_but_is_not_healthy(self):
        handle = self.acquire()
        inode = Path(handle.name).stat().st_ino
        self.locks.remove(handle)
        release_account_lock(handle)
        self.assertEqual(Path(handle.name).stat().st_ino, inode)
        self.assertFalse(account_lock_health(handle.name, os.getpid()))
        replacement = self.acquire()
        self.assertNotEqual(replacement.instance_id, handle.instance_id)
        self.assertFalse(account_lock_health(replacement.name, os.getpid(), expected_instance=handle.instance_id))

    def test_canonical_runtime_binding_prevents_sequential_split_state(self):
        handle = self.acquire(runtime_dir=Path(self.temp.name) / "A")
        self.locks.remove(handle)
        release_account_lock(handle)
        with self.assertRaisesRegex(RuntimeError, "canonical runtime"):
            self.acquire(runtime_dir=Path(self.temp.name) / "B")
        self.acquire(runtime_dir=Path(self.temp.name) / "A")

    def test_metadata_does_not_contain_plaintext_account(self):
        account = "S12345678901"
        handle = self.acquire(account)
        raw = Path(handle.name).read_text()
        self.assertNotIn(account, raw)
        self.assertNotIn(account[1:], raw)
        self.assertEqual(json.loads(raw)["pid"], os.getpid())

    def test_symlink_or_permissive_root_is_rejected(self):
        real = Path(self.temp.name) / "real"
        real.mkdir(mode=0o700)
        self.root.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "owner-only"):
            self.acquire()
        self.root.unlink()
        self.root.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, "owner-only"):
            self.acquire()

    def test_wrong_pid_or_untrusted_lock_path_is_not_healthy(self):
        handle = self.acquire()
        self.assertFalse(account_lock_health(handle.name, 2_147_483_647))
        self.assertFalse(account_lock_health(self.root / "not-an-account-lock", os.getpid()))

    def test_overflowing_pid_is_unknown_not_a_health_probe_exception(self):
        handle = self.acquire()
        for pid in (float("inf"), 10 ** 100):
            with self.subTest(pid=pid):
                self.assertFalse(account_lock_health(handle.name, pid))
