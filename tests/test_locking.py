"""Cross-process and in-process tests for ForgeAgent advisory locks."""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
from pathlib import Path

import pytest

from forge_agent.locking import LockUnavailable, acquire_locks


def _use_temp_root(temp_root: str) -> None:
    tempfile.tempdir = temp_root


def _hold_resource_lock(
    resource: str,
    temp_root: str,
    ready: object,
    release: object,
) -> None:
    _use_temp_root(temp_root)
    with acquire_locks([Path(resource)], timeout=5):
        ready.set()  # type: ignore[attr-defined]
        release.wait(10)  # type: ignore[attr-defined]


def _crash_while_holding_lock(resource: str, temp_root: str, ready: object) -> None:
    _use_temp_root(temp_root)
    with acquire_locks([Path(resource)], timeout=5):
        ready.set()  # type: ignore[attr-defined]
        os._exit(17)


def test_distinct_resources_do_not_block_each_other(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_resource_lock,
        args=(str(first), str(temp_root), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10)
        with acquire_locks([second], timeout=0):
            pass
        with pytest.raises(LockUnavailable), acquire_locks([first], timeout=0):
            pytest.fail("a second process acquired an already-held resource")
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_process_exit_releases_lock_automatically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    resource = tmp_path / "crash.py"
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    process = context.Process(
        target=_crash_while_holding_lock,
        args=(str(resource), str(temp_root), ready),
    )
    process.start()
    assert ready.wait(10)
    process.join(10)
    assert process.exitcode == 17

    with acquire_locks([resource], timeout=1):
        pass


def test_reverse_order_multi_resource_acquisition_does_not_deadlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    start = threading.Barrier(2)
    completed: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int, resources: list[Path]) -> None:
        try:
            start.wait(timeout=2)
            with acquire_locks(resources, timeout=2):
                completed.append(index)
        except BaseException as error:
            errors.append(error)

    workers = [
        threading.Thread(target=worker, args=(0, [first, second])),
        threading.Thread(target=worker, args=(1, [second, first])),
    ]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(5)

    assert all(not thread.is_alive() for thread in workers)
    assert errors == []
    assert sorted(completed) == [0, 1]


def test_timeout_zero_is_fail_fast_and_empty_resource_set_is_allowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    resource = tmp_path / "same.py"
    ready = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with acquire_locks([resource]):
            ready.set()
            release.wait(5)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(2)
        with pytest.raises(LockUnavailable), acquire_locks([resource], timeout=0):
            pytest.fail("timeout=0 must not wait for a held lock")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    with acquire_locks([], timeout=0):
        pass


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected(timeout: float) -> None:
    with (
        pytest.raises(ValueError, match="finite, non-negative"),
        acquire_locks([Path("resource")], timeout=timeout),
    ):
        pass
