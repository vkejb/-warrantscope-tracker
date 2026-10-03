"""Synthetic account singleton tests; no application locks or broker access."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
from unittest import TestCase
from unittest.mock import patch

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

    def released_provenance(self, *, runtime_dir=None):
        handle = self.acquire(runtime_dir=runtime_dir)
        path = Path(handle.name)
        raw = path.read_text()
        self.locks.remove(handle)
        release_account_lock(handle)
        return path, raw

    def test_incomplete_record_cannot_erase_previous_runtime_binding(self):
        path, raw = self.released_provenance(runtime_dir=Path(self.temp.name) / "A")
        original = json.loads(raw)
        incomplete = [{}, {"runtime_dir": ""}]
        incomplete.extend(
            {key: value for key, value in original.items() if key != missing}
            for missing in original
        )
        for record in incomplete:
            with self.subTest(fields=sorted(record)):
                corrupt = json.dumps(record)
                path.write_text(corrupt)
                with self.assertRaisesRegex(RuntimeError, "provenance.*manual review"):
                    self.acquire(runtime_dir=Path(self.temp.name) / "B")
                self.assertEqual(path.read_text(), corrupt)
        path.write_text(raw)
        with self.assertRaisesRegex(RuntimeError, "canonical runtime"):
            self.acquire(runtime_dir=Path(self.temp.name) / "B")
        self.acquire(runtime_dir=Path(self.temp.name) / "A")

    def test_malformed_or_conflicting_identity_is_never_overwritten(self):
        path, raw = self.released_provenance(runtime_dir=Path(self.temp.name) / "A")
        original = json.loads(raw)
        invalid = {
            "version": (True, "1", 0, 2),
            "pid": (True, 0, -1, 1.5, "1"),
            "instance_id": (None, "", "INVALID", "0" * 31),
            "environment": (None, "prod", "OTHER", "UAT", []),
            "account_fingerprint": (None, "", "X" * 12, "0" * 12),
            "acquired_at": (None, "", "invalid", "2026-10-03T09:00:00"),
            "runtime_dir": (None, 123, "relative", str(Path(self.temp.name) / "A" / ".." / "A")),
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    corrupt = json.dumps({**original, field: value})
                    path.write_text(corrupt)
                    with self.assertRaisesRegex(RuntimeError, "provenance.*manual review"):
                        self.acquire(runtime_dir=Path(self.temp.name) / "A")
                    self.assertEqual(path.read_text(), corrupt)

    def test_non_object_duplicate_or_oversized_provenance_is_rejected(self):
        path, raw = self.released_provenance(runtime_dir=Path(self.temp.name) / "A")
        duplicate = raw.rstrip()[:-1] + ', "runtime_dir": ""}'
        deeply_nested = "[" * 1100 + "0" + "]" * 1100
        for corrupt in ("[]", "null", "not-json", duplicate, deeply_nested, raw + " " * 8193):
            with self.subTest(kind=corrupt[:12]):
                path.write_text(corrupt)
                with self.assertRaisesRegex(RuntimeError, "provenance.*manual review"):
                    self.acquire(runtime_dir=Path(self.temp.name) / "A")
                self.assertEqual(path.read_text(), corrupt)

    def test_existing_empty_crash_inode_is_not_first_acquisition(self):
        path, _raw = self.released_provenance()
        inode = path.stat().st_ino
        path.write_text("")
        with self.assertRaisesRegex(RuntimeError, "provenance is empty.*manual review"):
            self.acquire()
        self.assertEqual(path.read_text(), "")
        self.assertEqual(path.stat().st_ino, inode)

    def test_omitted_runtime_argument_preserves_previous_binding(self):
        runtime_a = Path(self.temp.name) / "A"
        path, _raw = self.released_provenance(runtime_dir=runtime_a)
        handle = self.acquire()
        self.assertEqual(json.loads(path.read_text())["runtime_dir"], str(runtime_a.resolve()))
        self.locks.remove(handle)
        release_account_lock(handle)
        with self.assertRaisesRegex(RuntimeError, "canonical runtime"):
            self.acquire(runtime_dir=Path(self.temp.name) / "B")
        self.acquire(runtime_dir=runtime_a)

    def test_valid_no_runtime_api_can_be_reused_then_bound_once(self):
        path, _raw = self.released_provenance()
        handle = self.acquire()
        self.assertEqual(json.loads(path.read_text())["runtime_dir"], "")
        self.locks.remove(handle)
        release_account_lock(handle)
        self.acquire(runtime_dir=Path(self.temp.name) / "A")
        self.assertEqual(json.loads(path.read_text())["runtime_dir"], str((Path(self.temp.name) / "A").resolve()))

    def test_health_probe_never_certifies_corrupt_provenance(self):
        handle = self.acquire(runtime_dir=Path(self.temp.name) / "A")
        path = Path(handle.name)
        original = json.loads(path.read_text())
        for corrupt in ({}, {**original, "runtime_dir": None}, {**original, "version": True},
                        {**original, "acquired_at": "2026-10-03T09:00:00"}):
            with self.subTest(fields=sorted(corrupt)):
                path.write_text(json.dumps(corrupt))
                self.assertFalse(account_lock_health(handle.name, os.getpid(), expected_instance=handle.instance_id))
        path.write_text(json.dumps(original))
        self.assertTrue(account_lock_health(handle.name, os.getpid(), expected_instance=handle.instance_id))

    def test_invalid_runtime_never_creates_or_opens_an_account_inode(self):
        loop = Path(self.temp.name) / "runtime-loop"
        loop.symlink_to(loop)
        for runtime in (123, [], loop):
            with self.subTest(kind=type(runtime).__name__), patch(
                "yuanta_live_runtime_v01.account_lock._open_lock_file"
            ) as opened:
                with self.assertRaises((TypeError, ValueError, OSError, RuntimeError)):
                    self.acquire(runtime_dir=runtime)
                opened.assert_not_called()
                self.assertFalse(self.root.exists())
