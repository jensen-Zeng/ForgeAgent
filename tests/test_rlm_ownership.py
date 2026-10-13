"""Exclusive lifetime ownership for persistent RLM sessions."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge_agent.config import load_settings
from forge_agent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    TextPart,
)
from forge_agent.locking import LockUnavailable, acquire_locks
from forge_agent.models import FakeModelProvider
from forge_agent.rlm import RLMApplication
from forge_agent.session import InputEnvelope, InputKind, SessionStore
from forge_agent.tools import ToolRegistry


def _application(workspace: Path, store: SessionStore) -> tuple[RLMApplication, FakeModelProvider]:
    settings = load_settings(
        cwd=workspace,
        cli_overrides={"workspace": {"allowed_root": workspace}},
        environ={},
    )
    provider = FakeModelProvider([])
    application = RLMApplication(
        settings=settings,
        provider=provider,
        store=store,
        registry_factory=ToolRegistry,
    )
    return application, provider


@pytest.fixture(autouse=True)
def isolated_lock_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    temp_root = tmp_path / "system-temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))


async def test_same_store_has_one_application_owner_until_shutdown(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    first, _ = _application(tmp_path, store)

    with pytest.raises(LockUnavailable):
        _application(tmp_path, store)

    await first.shutdown()
    second, _ = _application(tmp_path, SessionStore.open(tmp_path, store.header.session_id))
    await second.shutdown()


async def test_distinct_root_sessions_can_be_owned_concurrently(tmp_path: Path) -> None:
    first_store = SessionStore.create(tmp_path)
    second_store = SessionStore.create(tmp_path)
    first, _ = _application(tmp_path, first_store)
    second, _ = _application(tmp_path, second_store)

    await first.shutdown()
    await second.shutdown()


def test_constructor_failure_releases_session_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore.create(tmp_path)
    settings = load_settings(
        cwd=tmp_path,
        cli_overrides={"workspace": {"allowed_root": tmp_path}},
        environ={},
    )

    def fail_runtime(self: RLMApplication, _: SessionStore) -> object:
        del self
        raise RuntimeError("simulated runtime construction failure")

    monkeypatch.setattr(RLMApplication, "_make_runtime", fail_runtime)
    with pytest.raises(RuntimeError, match="simulated runtime construction failure"):
        RLMApplication(
            settings=settings,
            provider=FakeModelProvider([]),
            store=store,
            registry_factory=ToolRegistry,
        )

    with acquire_locks([store.directory / "runtime-owner"], timeout=0):
        pass


async def test_shutdown_releases_ownership_even_if_child_cancellation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore.create(tmp_path)
    application, provider = _application(tmp_path, store)

    async def fail_cancel(*, timeout_seconds: float = 10.0) -> None:
        del timeout_seconds
        raise RuntimeError("simulated child shutdown failure")

    monkeypatch.setattr(application.child_manager, "cancel_all", fail_cancel)

    with pytest.raises(RuntimeError, match="simulated child shutdown failure"):
        await application.shutdown()
    assert provider.closed is True
    with acquire_locks([store.directory / "runtime-owner"], timeout=0):
        pass
    await application.shutdown()
    with pytest.raises(RuntimeError, match="session_closed"):
        await application.resume()
    with pytest.raises(RuntimeError, match="session_closed"):
        await application.submit_and_run("late task")


async def test_shutdown_waits_for_active_root_turn_and_leaves_pending_input_for_resume(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path)
    settings = load_settings(
        cwd=tmp_path,
        cli_overrides={"workspace": {"allowed_root": tmp_path}},
        environ={},
    )

    class BlockingProvider(FakeModelProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.entered = asyncio.Event()
            self.cancellation_seen = asyncio.Event()
            self.finish_cleanup = asyncio.Event()

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
            del request
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellation_seen.set()
                await self.finish_cleanup.wait()
                raise
            if False:
                yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text="unused")

    provider = BlockingProvider()
    application = RLMApplication(
        settings=settings,
        provider=provider,
        store=store,
        registry_factory=ToolRegistry,
    )
    first = InputEnvelope(text="long task")
    pending = InputEnvelope(kind=InputKind.FOLLOW_UP, text="run after recovery")
    await application.root_runtime.submit(first)
    await application.root_runtime.submit(pending)
    active_turn = asyncio.create_task(application.resume())
    await asyncio.wait_for(provider.entered.wait(), timeout=2)

    shutting_down = asyncio.create_task(application.shutdown())
    await asyncio.wait_for(provider.cancellation_seen.wait(), timeout=2)
    with (
        pytest.raises(LockUnavailable),
        acquire_locks([store.directory / "runtime-owner"], timeout=0),
    ):
        pass

    shutting_down.cancel()
    shutdown_result = await asyncio.gather(shutting_down, return_exceptions=True)
    assert isinstance(shutdown_result[0], asyncio.CancelledError)
    with (
        pytest.raises(LockUnavailable),
        acquire_locks([store.directory / "runtime-owner"], timeout=0),
    ):
        pass

    provider.finish_cleanup.set()
    result = await asyncio.gather(active_turn, return_exceptions=True)
    assert isinstance(result[0], tuple)
    assert len(result[0]) == 1
    assert result[0][0].status.value == "cancelled"
    with (
        pytest.raises(LockUnavailable),
        acquire_locks([store.directory / "runtime-owner"], timeout=0),
    ):
        pass
    await asyncio.wait_for(application.shutdown(), timeout=2)

    entries = store.replay()
    started_ids = {
        str(entry.payload.get("input_id")) for entry in entries if entry.type == "input_started"
    }
    consumed_ids = {
        str(entry.payload.get("input_id")) for entry in entries if entry.type == "input_consumed"
    }
    assert str(first.id) in started_ids | consumed_ids
    assert str(pending.id) not in started_ids | consumed_ids

    resumed_provider = FakeModelProvider([ModelResponse(content=(TextPart(text="resumed"),))])
    resumed = RLMApplication(
        settings=settings,
        provider=resumed_provider,
        store=SessionStore.open(tmp_path, store.header.session_id),
        registry_factory=ToolRegistry,
    )
    resumed_results = await resumed.resume()
    assert len(resumed_results) == 1
    assert resumed_results[0].final_text == "resumed"
    await resumed.shutdown()
    with acquire_locks([store.directory / "runtime-owner"], timeout=0):
        pass
