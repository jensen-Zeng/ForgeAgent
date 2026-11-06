"""Streaming progress is observable before completion without exposing fragments."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from forge_agent.config import load_settings
from forge_agent.domain import (
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    RunConfig,
    RunStatus,
    TextPart,
    ToolCall,
    ToolCallPart,
)
from forge_agent.engine import CancellationScope, ConversationContext, LoopAgentEngine
from forge_agent.models import FakeDelay, FakeModelProvider
from forge_agent.rlm import RLMApplication
from forge_agent.runtime import AgentRuntime
from forge_agent.session import SessionStore
from forge_agent.tools import ReadFileTool, ToolRegistry, Workspace
from forge_agent.tracing import EventBus, EventKind, MemoryEventSink, RunTracer


def text_delta(text: str) -> ModelStreamEvent:
    return ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text=text)


@pytest.mark.asyncio
async def test_runtime_progress_precedes_completion_and_omits_secret_fragments(
    tmp_path: Path,
) -> None:
    observed = asyncio.Event()
    sink = MemoryEventSink()
    config = RunConfig(task="inspect", workspace=tmp_path)
    provider = FakeModelProvider(
        [
            text_delta("sk-split-"),
            text_delta("secret-123456"),
            ModelStreamEvent(
                type=ModelStreamEventType.TOOL_ARGUMENT_DELTA,
                tool_index=0,
                arguments_delta='{"api_key":"splitsecret"}',
            ),
            FakeDelay(0.05),
            ModelResponse(content=(TextPart(text="Done"),)),
        ]
    )
    runtime = AgentRuntime(
        provider=provider,
        tools=ToolRegistry(),
        streaming=True,
        tracer=RunTracer(run_id=config.run_id, bus=EventBus((sink,))),
        event_sink=lambda kind, data: observed.set() if kind is EventKind.MODEL_DELTA else None,
    )
    task = asyncio.create_task(runtime.run(config))
    await asyncio.wait_for(observed.wait(), timeout=2)
    assert not task.done()
    result = await task
    assert result.status is RunStatus.COMPLETED
    deltas = [event for event in sink.events if event.kind is EventKind.MODEL_DELTA]
    assert len(deltas) == 1  # High-frequency fragments are coalesced.
    assert deltas[0].data["text_chars"] == len("sk-split-")
    serialized = "\n".join(event.model_dump_json() for event in sink.events)
    for fragment in ("sk-split-", "secret-123456", "splitsecret", "arguments_delta"):
        assert fragment not in serialized


@pytest.mark.asyncio
async def test_rlm_progress_and_tool_lifecycle_are_persisted_and_correlated(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("example", encoding="utf-8")
    store = SessionStore.create(tmp_path)
    events: list[tuple[UUID, str, dict[str, object]]] = []
    provider = FakeModelProvider(
        [
            text_delta("Inspecting"),
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="read-1",
                            name="read_file",
                            arguments={"path": "README.md"},
                        )
                    ),
                )
            ),
            text_delta("Finished"),
            ModelResponse(content=(TextPart(text="Read the file"),)),
        ]
    )
    application = RLMApplication(
        settings=load_settings(cwd=tmp_path, environ={}),
        provider=provider,
        store=store,
        registry_factory=lambda: ToolRegistry((ReadFileTool(Workspace(tmp_path)),)),
        progress_observer=lambda sid, kind, data: events.append((sid, kind, data)),
    )
    try:
        results = await application.submit_and_run("Read README.md")
        assert results[0].status is RunStatus.COMPLETED
    finally:
        await application.shutdown()
    assert provider.closed
    assert all(sid == store.header.session_id for sid, _, _ in events)
    kinds = [kind for _, kind, _ in events]
    assert kinds.index("model_delta") < kinds.index("tool_call_requested")
    assert kinds.index("tool_call_requested") < kinds.index("tool_call_completed")
    assert kinds[-1] == "turn_completed"
    entries = [entry for entry in store.replay() if entry.type == "runtime_event"]
    requested = next(
        entry.payload["data"] for entry in entries if entry.payload["kind"] == "tool_call_requested"
    )
    completed = next(
        entry.payload["data"] for entry in entries if entry.payload["kind"] == "tool_call_completed"
    )
    assert isinstance(requested, dict) and isinstance(completed, dict)
    assert requested["turn_id"] == completed["turn_id"]
    assert requested["call_id"] == completed["call_id"] == "read-1"
    assert "arguments" not in requested


@pytest.mark.asyncio
async def test_stream_cancellation_emits_terminal_turn_event(tmp_path: Path) -> None:
    provider = FakeModelProvider([text_delta("working"), FakeDelay(60)])
    scope = CancellationScope()
    received = asyncio.Event()
    kinds: list[EventKind] = []

    def observe(kind: EventKind, data: dict[str, object]) -> None:
        kinds.append(kind)
        if kind is EventKind.MODEL_DELTA:
            received.set()

    context = ConversationContext.start(RunConfig(task="inspect", workspace=tmp_path))
    task = asyncio.create_task(
        LoopAgentEngine(provider).run_turn(
            context,
            ToolRegistry(),
            observe,
            scope,
        )
    )
    await asyncio.wait_for(received.wait(), timeout=2)
    scope.cancel("test_cancel")
    result = await asyncio.wait_for(task, timeout=2)
    assert result.status is RunStatus.CANCELLED
    assert kinds[-1] is EventKind.TURN_CANCELLED


@pytest.mark.asyncio
async def test_broken_progress_display_does_not_abort_durable_turn(tmp_path: Path) -> None:
    def broken_display(sid: UUID, kind: str, data: dict[str, object]) -> None:
        raise OSError("terminal disconnected")

    application = RLMApplication(
        settings=load_settings(cwd=tmp_path, environ={}),
        provider=FakeModelProvider(
            [text_delta("ok"), ModelResponse(content=(TextPart(text="ok"),))]
        ),
        store=SessionStore.create(tmp_path),
        registry_factory=ToolRegistry,
        progress_observer=broken_display,
    )
    try:
        results = await application.submit_and_run("inspect")
        assert results[0].status is RunStatus.COMPLETED
    finally:
        await application.shutdown()
