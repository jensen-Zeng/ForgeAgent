"""Cross-process advisory locks for canonical filesystem resources.

Lock files live in the per-user temporary directory and are deliberately never
removed. Unlinking a lock file can split waiters across different inodes.
"""

from __future__ import annotations

import errno
import hashlib
import importlib
import math
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    _fcntl = importlib.import_module("fcntl")


class LockUnavailable(TimeoutError):
    """Raised when one or more requested resources cannot be locked in time."""


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_POLL_INTERVAL_SECONDS = 0.01


def _canonical_resource(resource: Path) -> str:
    if not isinstance(resource, Path):
        raise TypeError("lock resources must be pathlib.Path instances")
    canonical = resource.expanduser().resolve(strict=False)
    value = os.path.normcase(str(canonical))
    return value.casefold() if os.name == "nt" else value


def _resource_key(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()


def _thread_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _lock_directory() -> Path:
    directory_name = "forge-agent-locks"
    if os.name != "nt":
        directory_name = f"{directory_name}-{os.getuid()}"  # type: ignore[attr-defined]
    directory = Path(tempfile.gettempdir()) / directory_name
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    except OSError as error:
        raise LockUnavailable("unable to create the per-user lock directory") from error

    try:
        info = directory.lstat()
    except OSError as error:
        raise LockUnavailable("unable to inspect the per-user lock directory") from error
    if stat.S_ISLNK(info.st_mode) or _is_junction(directory):
        raise LockUnavailable("the per-user lock directory must not be a link")
    if not stat.S_ISDIR(info.st_mode):
        raise LockUnavailable("the per-user lock path is not a directory")

    if os.name != "nt" and (
        info.st_uid != os.getuid()  # type: ignore[attr-defined]
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise LockUnavailable("the per-user lock directory has unsafe ownership or permissions")
    return directory


def _is_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if is_junction is not None else False


def _open_lock_file(directory: Path, key: str) -> int:
    path = directory / f"{key}.lock"
    if path.is_symlink() or _is_junction(path):
        raise LockUnavailable("a lock file path must not be a link")

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        flags |= os.O_APPEND
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise LockUnavailable("unable to open a per-resource lock file") from error

    try:
        info = os.fstat(descriptor)
        path_info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(path_info.st_mode):
            raise LockUnavailable("a lock file must be a regular file")
        if (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise LockUnavailable("a lock file changed while it was being opened")
        if info.st_nlink != 1:
            raise LockUnavailable("a lock file must not have multiple hard links")
        if os.name != "nt" and (
            info.st_uid != os.getuid()  # type: ignore[attr-defined]
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise LockUnavailable("a lock file has unsafe ownership or permissions")
        else:
            os.chmod(path, 0o600)

        if os.name == "nt" and info.st_size == 0:
            # msvcrt.locking requires a byte range that exists. O_APPEND makes
            # simultaneous first-time initialization harmless.
            os.lseek(descriptor, 0, os.SEEK_END)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_os_lock(descriptor: int) -> bool:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EINTR}:
                return False
            # Windows reports a sharing/lock violation with platform-specific
            # WinError values that do not always map to EACCES.
            if getattr(error, "winerror", None) in {32, 33, 36}:
                return False
            raise
        return True

    try:
        _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EINTR}:
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    if os.name == "nt":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)


@contextmanager
def acquire_locks(resources: Iterable[Path], *, timeout: float = 5.0) -> Iterator[None]:
    """Acquire sorted thread- and process-level locks for filesystem resources.

    The locks coordinate ForgeAgent processes that use this API. They are
    advisory: unrelated applications and editors do not participate.
    """
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite, non-negative number")

    keys = sorted({_resource_key(_canonical_resource(resource)) for resource in resources})
    if not keys:
        yield
        return

    deadline = time.monotonic() + timeout
    acquired_threads: list[threading.Lock] = []
    descriptors: list[int] = []
    locked_descriptors: list[int] = []
    try:
        for key in keys:
            local_lock = _thread_lock(key)
            remaining = max(0.0, deadline - time.monotonic())
            if not local_lock.acquire(timeout=remaining):
                raise LockUnavailable("timed out waiting for a resource lock")
            acquired_threads.append(local_lock)

        directory = _lock_directory()
        for key in keys:
            descriptor = _open_lock_file(directory, key)
            descriptors.append(descriptor)
            while True:
                try:
                    acquired = _try_os_lock(descriptor)
                except OSError as error:
                    raise LockUnavailable("unable to acquire a resource lock") from error
                if acquired:
                    locked_descriptors.append(descriptor)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockUnavailable("timed out waiting for a resource lock")
                time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

        yield
    finally:
        try:
            for descriptor in reversed(locked_descriptors):
                with suppress(OSError):
                    _unlock(descriptor)
                    # Closing the descriptor also releases the OS lock.
            for descriptor in reversed(descriptors):
                with suppress(OSError):
                    os.close(descriptor)
        finally:
            for local_lock in reversed(acquired_threads):
                local_lock.release()
