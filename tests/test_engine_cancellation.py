"""Cancellation must finish owned work before callers release session resources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from forge_agent.children import ChildManager, ChildState
from forge_agent.domain import ModelRequest, ModelStreamEvent, RunConfig
from forge_agent.engine import CancellationScope, LoopAgentEngine
from forge_agent.models import FakeModelProvider
from forge_agent.session import SessionRuntime, SessionStore
from forge_agent.tools import ToolRegistry


@pytest.mark.parametrize("signal_scope", [False, True])
async def test_cancelling_scope_runner_waits_for_operation_cleanup(signal_scope: bool) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def operation() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            await asyncio.sleep(0)
            cleaned_up.set()

    scope = CancellationScope()
    operation_task = asyncio.create_task(operation())
    runner = asyncio.create_task(scope.run(operation_task))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if signal_scope:
            scope.cancel()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert operation_task.done()
        assert cleaned_up.is_set()
    finally:
        release.set()
        await asyncio.gather(operation_task, runner, return_exceptions=True)


async def test_child_cancel_waits_for_real_engine_provider_cleanup(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned_up = asyncio.Event()

    class BlockingProvider(FakeModelProvider):
        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
            entered.set()
            try:
                await release.wait()
                async for event in super().stream(request):
                    yield event
            finally:
                await asyncio.sleep(0)
                cleaned_up.set()

    provider = BlockingProvider([])
    root = SessionStore.create(tmp_path)

    def factory(store: SessionStore) -> SessionRuntime:
        return SessionRuntime(
            store=store,
            engine=LoopAgentEngine(provider),
            tools=ToolRegistry(),
            base_config=RunConfig(task="base", workspace=tmp_path),
        )

    manager = ChildManager(parent_store=root, root_store=root, runtime_factory=factory)
    child = manager.spawn("cancel-test", "wait for cancellation")
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        child.cancel()
        result = await asyncio.wait_for(child.wait(), timeout=2)
        assert result.state is ChildState.CANCELLED
        assert cleaned_up.is_set()
    finally:
        release.set()
        await asyncio.wait_for(cleaned_up.wait(), timeout=2)
        await manager.cancel_all()
