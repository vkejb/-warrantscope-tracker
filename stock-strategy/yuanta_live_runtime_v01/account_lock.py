"""Account/environment OS singleton shared by all checkouts on one host.

No broker or credential access occurs here. The common lock directory is not
derived from TMPDIR or a checkout, and plaintext account numbers are never
written. Lock files are never unlinked: unlinking a held inode could permit a
second controller. Tests must explicitly supply a temporary lock_root.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import TextIO
import uuid


class AccountExecutionLock:
    def __init__(self, handle: TextIO, path: Path, instance_id: str):
        self._handle = handle
        self.name = str(path)
        self.instance_id = instance_id

    def fileno(self):
        return self._handle.fileno()

    def close(self):
        self._handle.close()

    def seek(self, *args):
        return self._handle.seek(*args)

    def read(self, *args):
        return self._handle.read(*args)


def _canonical_account(account: str) -> str:
    value = str(account).strip().upper().replace("-", "").replace(" ", "")
    if re.fullmatch(r"S[0-9]{11}", value):
        value = value[1:]
    if not value or len(value) > 128 or not re.fullmatch(r"[A-Z0-9_]+", value):
        raise ValueError("account identity is invalid for execution locking")
    return value


def _validate_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise RuntimeError("execution lock directory must be owner-only and not a symlink")


def _open_lock_file(path: Path, *, create: bool) -> TextIO:
    flags = os.O_RDWR if create else os.O_RDONLY
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise RuntimeError("execution lock file must be owner-only and regular")
        return os.fdopen(fd, "r+" if create else "r", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise


def acquire_account_lock(
    account: str,
    environment: str,
    *,
    lock_root: Path | None = None,
    runtime_dir: Path | None = None,
) -> AccountExecutionLock:
    account_identity = _canonical_account(account)
    env = str(environment).strip().upper()
    if env not in {"PROD", "UAT"}:
        raise ValueError("execution lock environment must be PROD or UAT")
    root = Path(lock_root) if lock_root is not None else Path("/tmp") / f"warrantscope-execution-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _validate_directory(root)
    digest = hashlib.sha256(f"{env}\0{account_identity}".encode()).hexdigest()
    path = root / f"account-{digest}.lock"
    handle = _open_lock_file(path, create=True)
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise RuntimeError("another controller owns the broker account execution lock") from None
            raise
        acquired = True
        old_text = handle.read(8193)
        if old_text:
            try:
                old = json.loads(old_text)
            except ValueError:
                raise RuntimeError("execution account lock provenance is unreadable; manual review required") from None
            if not isinstance(old, dict):
                raise RuntimeError("execution account lock provenance is invalid")
            old_runtime = str(old.get("runtime_dir", ""))
            if runtime_dir is not None and old_runtime and old_runtime != str(Path(runtime_dir).resolve()):
                raise RuntimeError("broker account is bound to another runtime directory; use the canonical runtime")
        instance = uuid.uuid4().hex
        row = {
            "version": 1,
            "pid": os.getpid(),
            "instance_id": instance,
            "environment": env,
            "account_fingerprint": hashlib.sha256(account_identity.encode()).hexdigest()[:12],
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "runtime_dir": str(Path(runtime_dir).resolve()) if runtime_dir is not None else "",
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return AccountExecutionLock(handle, path, instance)
    except Exception:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        raise


def release_account_lock(handle: AccountExecutionLock | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def account_lock_health(
    path: str | Path,
    expected_pid: int,
    *,
    expected_instance: str | None = None,
) -> bool:
    """Read-only ownership probe, not proof that the broker position is flat."""
    try:
        lock_path = Path(path)
        if not re.fullmatch(r"account-[0-9a-f]{64}\.lock", lock_path.name):
            return False
        _validate_directory(lock_path.parent)
        pid = int(expected_pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        with _open_lock_file(lock_path, create=False) as handle:
            row = json.loads(handle.read(8193))
            if not isinstance(row, dict) or int(row.get("pid", 0)) != pid:
                return False
            instance = row.get("instance_id")
            if not instance or (expected_instance is not None and expected_instance != instance):
                return False
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    return False
                handle.seek(0)
                fresh = json.loads(handle.read(8193))
                return fresh.get("pid") == pid and fresh.get("instance_id") == instance
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
    except (OSError, ValueError, TypeError, AttributeError, OverflowError):
        return False
