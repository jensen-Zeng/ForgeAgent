from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import JsonValue

from forge_agent.children import ChildManager, ChildState
from forge_agent.compaction import CompactionFailedError, CompactionPolicy, ContextBuilder
from forge_agent.config import load_settings
from forge_agent.domain import (
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunConfig,
    RunMetrics,
    RunStatus,
    StopReason,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
    Usage,
)
from forge_agent.engine import (
    CancellationScope,
    ConversationContext,
    LoopAgentEngine,
    TurnResult,
    TurnStopReason,
)
from forge_agent.kernel import HostBridge, HostRequest, KernelManager
from forge_agent.messages import AgentMessageRouter
from forge_agent.models import FakeDelay, FakeModelProvider, OpenAICompatibleProvider
from forge_agent.models.base import ProviderResponseError
from forge_agent.rlm import (
    BudgetedSessionProvider,
    RLMApplication,
    SharedBudgetExceeded,
    UsageLedger,
)
from forge_agent.session import InputEnvelope, InputKind, SessionRuntime, SessionStore
from forge_agent.tools import ToolRegistry


class RecordingEngine:
    def __init__(self, *, delay: float = 0) -> None:
        self.prompts: list[str] = []
        self.delay = delay

    async def run_turn(
        self,
        context: ConversationContext,
        tools: ToolRegistry,
        event_sink: object,
        cancel_scope: CancellationScope,
    ) -> TurnResult:
        del tools, event_sink
        prompt = next(
            part.text
            for part in reversed(context.messages[-1].content)
            if isinstance(part, TextPart)
        )
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        assistant = Message(role=Role.ASSISTANT, content=(TextPart(text=f"done:{prompt}"),))
        return TurnResult(
            session_id=context.session_id,
            status=RunStatus.COMPLETED,
            stop_reason=TurnStopReason.COMPLETED,
            run_stop_reason=StopReason.COMPLETED,
            messages=(*context.messages, assistant),
            final_text=f"done:{prompt}",
            metrics=RunMetrics(),
        )


def make_runtime(store: SessionStore, engine: RecordingEngine) -> SessionRuntime:
    return SessionRuntime(
        store=store,
        engine=engine,
        tools=ToolRegistry(),
        base_config=RunConfig(task="base", workspace=Path(store.header.workspace)),
    )


@pytest.mark.asyncio
async def test_follow_up_queue_is_durable_ordered_and_exactly_once(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    engine = RecordingEngine()
    runtime = make_runtime(store, engine)
    envelopes = [
        InputEnvelope(kind=InputKind.FOLLOW_UP, text=f"item-{index}") for index in range(20)
    ]
    accepted = await asyncio.gather(*(runtime.submit(envelope) for envelope in envelopes))
    assert [item.receipt_sequence for item in accepted] == list(range(1, 21))

    results = await runtime.run_until_idle()
    assert len(results) == 20
    assert engine.prompts == [f"item-{index}" for index in range(20)]

    reloaded_engine = RecordingEngine()
    reloaded = make_runtime(SessionStore.open(tmp_path, runtime.session_id), reloaded_engine)
    assert await reloaded.run_until_idle() == ()
    assert reloaded_engine.prompts == []


@pytest.mark.asyncio
async def test_steer_waits_for_model_boundary_then_merges_original_task(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        [
            FakeDelay(0.05),
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="would-run",
                            name="missing_tool",
                            arguments={},
                        )
                    ),
                )
            ),
            ModelResponse(content=(TextPart(text="redirected"),)),
        ]
    )
    store = SessionStore.create(tmp_path)
    runtime = SessionRuntime(
        store=store,
        engine=LoopAgentEngine(provider),
        tools=ToolRegistry(),
        base_config=RunConfig(task="base", workspace=tmp_path),
    )
    await runtime.submit(InputEnvelope(text="original"))
    runner = asyncio.create_task(runtime.run_until_idle())
    await asyncio.sleep(0.01)
    await runtime.submit(InputEnvelope(kind=InputKind.STEER, text="new direction"))
    results = await runner

    assert [result.status for result in results] == [RunStatus.CANCELLED, RunStatus.COMPLETED]
    second_request = provider.requests[-1]
    user_part = second_request.messages[-1].content[0]
    assert isinstance(user_part, TextPart)
    assert "original" in user_part.text
    assert "new direction" in user_part.text


def test_session_tail_requires_explicit_repair_and_preserves_unicode(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    store.append("future_entry", {"text": "中文🙂\\path"})
    with store.path.open("ab") as stream:
        stream.write(b'{"partial":')
    inspection = store.inspect_tail()
    assert not inspection.valid
    assert inspection.last_good_offset < inspection.file_size
    assert store.repair_tail().file_size == inspection.file_size
    repaired = store.repair_tail(truncate_tail=True)
    assert repaired.valid
    assert store.replay()[0].payload["text"] == "中文🙂\\path"


@pytest.mark.asyncio
async def test_hundred_turn_reload_restores_identical_leaf_context(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    runtime = make_runtime(store, RecordingEngine())
    for index in range(100):
        await runtime.submit(InputEnvelope(text=f"turn-{index}"))
    results = await runtime.run_until_idle()
    assert len(results) == 100

    reloaded = make_runtime(SessionStore.open(tmp_path, runtime.session_id), RecordingEngine())
    assert reloaded.last_result == results[-1]
    assert await reloaded.run_until_idle() == ()


@pytest.mark.asyncio
async def test_kernel_persists_namespace_recovers_after_interrupt_and_artifacts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "中文 workspace"
    workspace.mkdir()
    session_id = uuid4()
    manager = KernelManager(
        workspace=workspace,
        session_id=session_id,
        root_session_id=session_id,
        artifacts=tmp_path / "artifacts",
        preview_bytes=4096,
    )
    try:
        first = await manager.execute("value = 40")
        second = await manager.execute("await __import__('asyncio').sleep(0); value + 2")
        assert first.ok
        assert second.ok and second.result == "42"

        await manager.execute("counter = 0")
        serial = await asyncio.gather(
            manager.execute("await __import__('asyncio').sleep(0.05); counter += 1; counter"),
            manager.execute("counter += 1; counter"),
        )
        assert [item.result for item in serial] == ["1", "2"]

        timeout = await manager.execute("while True: pass", timeout_seconds=0.2)
        assert timeout.timed_out
        recovered = await manager.execute("1 + 1")
        assert recovered.ok and recovered.result == "2"

        large = await manager.execute("'界' * 4_000_000", timeout_seconds=30)
        assert large.truncated
        assert large.artifact is not None
        assert Path(large.artifact).stat().st_size > 10_000_000

        crashed = await manager.execute("__import__('os')._exit(7)", timeout_seconds=3)
        assert not crashed.ok and crashed.restarted
        rebuilt = await manager.execute("1 + 1")
        assert rebuilt.ok and rebuilt.result == "2"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_host_bridge_rejects_unknown_request_and_unknown_fields(tmp_path: Path) -> None:
    bridge = HostBridge(tmp_path / "bridge", timeout_seconds=0.1)
    response = await bridge.dispatch(HostRequest(operation="unknown"))
    assert not response.ok and response.error_code == "unknown_host_request"
    with pytest.raises(ValueError):
        HostRequest.model_validate(
            {"id": str(uuid4()), "operation": "unknown", "payload": {}, "session_id": "spoof"}
        )


@pytest.mark.asyncio
async def test_kernel_proxy_round_trips_through_bound_host_bridge(tmp_path: Path) -> None:
    bridge = HostBridge(tmp_path / "bridge")

    async def list_handler(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
        assert payload == {}
        return {"children": ["a"]}

    bridge.register("rlm.list", list_handler)
    session_id = uuid4()
    manager = KernelManager(
        workspace=tmp_path,
        session_id=session_id,
        root_session_id=session_id,
        artifacts=tmp_path / "artifacts",
        host_bridge=bridge,
    )
    try:
        result = await manager.execute("rlm.list()")
        assert result.ok
        assert result.result == "{'children': ['a']}"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_three_children_return_handles_and_run_independently(tmp_path: Path) -> None:
    root = SessionStore.create(tmp_path)
    engines: dict[UUID, RecordingEngine] = {}

    def factory(store: SessionStore) -> SessionRuntime:
        engine = RecordingEngine(delay=0.05)
        engines[store.header.session_id] = engine
        return make_runtime(store, engine)

    manager = ChildManager(
        parent_store=root,
        root_store=root,
        runtime_factory=factory,
        max_concurrent=4,
    )
    handles = [manager.spawn(f"child-{index}", f"task-{index}") for index in range(3)]
    assert all(handle.info.state is ChildState.ADMITTED for handle in handles)
    await asyncio.gather(*(handle.wait() for handle in handles))
    assert all(handle.info.state is ChildState.COMPLETED for handle in handles)
    assert {handle.info.final_text for handle in handles} == {
        "done:task-0",
        "done:task-1",
        "done:task-2",
    }
    assert len(engines) == 3
    with pytest.raises(ValueError, match="duplicate_child_name"):
        manager.spawn("child-0", "again")


@pytest.mark.asyncio
async def test_parent_cancel_propagates_to_all_active_children(tmp_path: Path) -> None:
    root = SessionStore.create(tmp_path)

    def factory(store: SessionStore) -> SessionRuntime:
        return make_runtime(store, RecordingEngine(delay=5))

    manager = ChildManager(
        parent_store=root,
        root_store=root,
        runtime_factory=factory,
        max_concurrent=4,
    )
    handles = [manager.spawn(f"slow-{index}", "wait") for index in range(2)]
    await asyncio.sleep(0.05)
    await manager.cancel_all(timeout_seconds=1)
    assert all(handle.info.state is ChildState.CANCELLED for handle in handles)


@pytest.mark.asyncio
async def test_agent_messages_persist_and_deliver_once_after_router_restart(
    tmp_path: Path,
) -> None:
    root = SessionStore.create(tmp_path)
    child_id = uuid4()
    child = SessionStore.create(
        tmp_path,
        session_id=child_id,
        root_session_id=root.header.root_session_id,
        parent_id=root.header.session_id,
        generation=1,
        directory=root.directory / "children" / str(child_id),
    )
    engine = RecordingEngine()
    runtime = make_runtime(child, engine)
    stores = {root.header.session_id: root, child_id: child}
    router = AgentMessageRouter(
        sender_store=root,
        stores=stores,
        runtimes={child_id: runtime},
    )
    message = router.send(child_id, "continue")
    restarted_router = AgentMessageRouter(
        sender_store=root,
        stores=stores,
        runtimes={child_id: runtime},
    )
    assert [item.id for item in restarted_router.list_pending(child_id)] == [message.id]
    assert await restarted_router.deliver_pending(child_id) == 1
    assert await restarted_router.deliver_pending(child_id) == 0
    await runtime.run_until_idle()
    assert engine.prompts == ["continue"]

    outsider = SessionStore.create(tmp_path / "other")
    stores[outsider.header.session_id] = outsider
    with pytest.raises(ValueError, match="cross_family_message_denied"):
        restarted_router.send(outsider.header.session_id, "denied")


@pytest.mark.asyncio
async def test_two_kernel_namespaces_are_isolated(tmp_path: Path) -> None:
    first_id = uuid4()
    second_id = uuid4()
    first = KernelManager(
        workspace=tmp_path,
        session_id=first_id,
        root_session_id=first_id,
        artifacts=tmp_path / "first-artifacts",
    )
    second = KernelManager(
        workspace=tmp_path,
        session_id=second_id,
        root_session_id=second_id,
        artifacts=tmp_path / "second-artifacts",
    )
    try:
        await asyncio.gather(
            first.execute("identity = 'first'"), second.execute("identity = 'second'")
        )
        results = await asyncio.gather(first.execute("identity"), second.execute("identity"))
        assert [result.result for result in results] == ["'first'", "'second'"]
    finally:
        await asyncio.gather(first.shutdown(), second.shutdown())


@pytest.mark.asyncio
async def test_openai_stream_aggregates_text_and_tool_argument_fragments() -> None:
    chunks = [
        {"id": "r1", "choices": [{"delta": {"content": "hi "}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c1",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"pa',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": 'th":"a.py"}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
    ]
    body = "\n\n".join([*("data: " + json.dumps(chunk) for chunk in chunks), "data: [DONE]"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
            request=request,
        )

    provider = OpenAICompatibleProvider(
        model_name="test",
        api_key="secret",
        base_url="https://example.test/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    message = Message(role=Role.USER, content=(TextPart(text="x"),))
    events = [event async for event in provider.stream(ModelRequest(messages=(message,)))]
    response = events[-1].response
    assert response is not None
    assert response.text_parts[0].text == "hi "
    assert response.tool_calls[0].arguments == {"path": "a.py"}
    assert response.usage.total_tokens == 7
    await provider.aclose()


@pytest.mark.asyncio
async def test_fake_provider_scripted_stream_delay_and_completion() -> None:
    response = ModelResponse(content=(TextPart(text="ok"),))
    provider = FakeModelProvider([response])
    message = Message(role=Role.USER, content=(TextPart(text="task"),))
    assert await provider.generate(ModelRequest(messages=(message,))) == response


@pytest.mark.asyncio
async def test_root_usage_ledger_aggregates_sessions_and_stops_next_request(
    tmp_path: Path,
) -> None:
    response = ModelResponse(
        content=(TextPart(text="ok"),),
        usage=Usage(input_tokens=3, output_tokens=3),
    )
    raw_provider = FakeModelProvider([response, response, response])
    root = SessionStore.create(tmp_path)
    ledger = UsageLedger(root)
    first = BudgetedSessionProvider(
        raw_provider,
        ledger=ledger,
        session_id=root.header.session_id,
        max_tokens=10,
        max_cost=None,
    )
    child_id = uuid4()
    second = BudgetedSessionProvider(
        raw_provider,
        ledger=ledger,
        session_id=child_id,
        max_tokens=10,
        max_cost=None,
    )
    message = Message(role=Role.USER, content=(TextPart(text="task"),))
    request = ModelRequest(messages=(message,))
    await first.generate(request)
    await second.generate(request)
    assert ledger.total_tokens == 12
    with pytest.raises(SharedBudgetExceeded):
        await first.generate(request)
    usage_entries = [entry for entry in root.replay() if entry.type == "usage"]
    assert [entry.payload["session_id"] for entry in usage_entries] == [
        str(root.header.session_id),
        str(child_id),
    ]


@pytest.mark.asyncio
async def test_context_compaction_persists_summary_and_keeps_recent_verbatim(
    tmp_path: Path,
) -> None:
    store = SessionStore.create(tmp_path)
    summary = "\n".join(
        f"## {heading}\nvalue"
        for heading in (
            "Goal",
            "Constraints",
            "Completed",
            "In Progress",
            "Key Decisions",
            "Failures and Evidence",
            "Next Steps",
            "Critical Files and Commands",
        )
    )
    provider = FakeModelProvider([ModelResponse(content=(TextPart(text=summary),))])
    builder = ContextBuilder(
        store=store,
        provider=provider,
        policy=CompactionPolicy(
            window_tokens=250,
            reserve_tokens=50,
            keep_recent_tokens=70,
            max_tool_result_chars=20,
        ),
    )
    messages = (
        Message(role=Role.SYSTEM, content=(TextPart(text="system"),)),
        Message(role=Role.USER, content=(TextPart(text="old " * 500),)),
        Message(
            role=Role.ASSISTANT,
            content=(
                ToolCallPart(
                    tool_call=ToolCall(
                        id="pair-1",
                        name="read_file",
                        arguments={"path": "very/long/" + "directory/" * 30 + "file.py"},
                    )
                ),
            ),
        ),
        Message(
            role=Role.TOOL,
            content=(
                ToolResultPart(
                    tool_result=ToolResult(
                        call_id="pair-1",
                        name="read_file",
                        ok=True,
                        output="evidence " * 20,
                    )
                ),
            ),
        ),
        Message(role=Role.USER, content=(TextPart(text="recent exact"),)),
    )
    compacted = await builder.prepare(messages)
    assert compacted[-1] == messages[-1]
    assert compacted[-3:-1] == messages[-3:-1]
    summary_part = compacted[1].content[0]
    assert isinstance(summary_part, TextPart)
    assert "Persistent session summary" in summary_part.text
    assert any(entry.type == "compaction" for entry in store.replay())


@pytest.mark.asyncio
async def test_compaction_failure_never_discards_overflowing_history(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    provider = FakeModelProvider([ProviderResponseError("failed")])
    builder = ContextBuilder(
        store=store,
        provider=provider,
        policy=CompactionPolicy(window_tokens=100, reserve_tokens=20, keep_recent_tokens=10),
    )
    messages = (
        Message(role=Role.SYSTEM, content=(TextPart(text="system"),)),
        Message(role=Role.USER, content=(TextPart(text="x" * 1_000),)),
        Message(role=Role.USER, content=(TextPart(text="recent"),)),
    )
    with pytest.raises(CompactionFailedError):
        await builder.prepare(messages)
    assert any(entry.type == "compaction_failed" for entry in store.replay())


@pytest.mark.asyncio
async def test_offline_rlm_end_to_end_executes_persistent_python_tool(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        [
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="python-1",
                            name="execute_python",
                            arguments={"code": "value = 40\nvalue + 2"},
                        )
                    ),
                )
            ),
            ModelResponse(content=(TextPart(text="Python returned 42."),)),
        ]
    )
    settings = load_settings(
        cwd=tmp_path,
        cli_overrides={"workspace": {"allowed_root": tmp_path}},
        environ={},
    )
    store = SessionStore.create(tmp_path)
    application = RLMApplication(
        settings=settings,
        provider=provider,
        store=store,
        registry_factory=ToolRegistry,
    )
    try:
        results = await application.submit_and_run("calculate")
        assert len(results) == 1
        assert results[0].final_text == "Python returned 42."
        events = [
            entry.payload.get("kind") for entry in store.replay() if entry.type == "runtime_event"
        ]
        assert "kernel_execution_started" in events
        assert "kernel_execution_completed" in events
    finally:
        await application.shutdown()
