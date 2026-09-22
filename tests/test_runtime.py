"""Deterministic integration tests for the bounded Agent Runtime."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import ClassVar, cast
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from forge_agent.domain import (
    Budget,
    ModelRequest,
    ModelResponse,
    Role,
    RunConfig,
    RunStatus,
    StopReason,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
    Usage,
)
from forge_agent.models import FakeModelProvider, ProviderResponseError
from forge_agent.runtime import AgentRuntime
from forge_agent.tools import (
    BaseTool,
    ReadFileTool,
    ToolExecution,
    ToolRegistry,
    Workspace,
)
from forge_agent.tracing import EventBus, EventKind, MemoryEventSink, RunTracer

RUN_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class ValueParameters(BaseModel):
    """Strict arguments for the deterministic recording tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    value: str


class RecordingTool(BaseTool[ValueParameters]):
    """Record invocation order and return the supplied value."""

    name = "record"
    description = "Record one value"
    parameters_type: ClassVar[type[BaseModel]] = ValueParameters

    def __init__(self, seen: list[str]) -> None:
        self._seen = seen

    def run(self, parameters: ValueParameters) -> ToolExecution:
        self._seen.append(parameters.value)
        return ToolExecution(output=parameters.value)


class BrokenTool(BaseTool[ValueParameters]):
    """Raise unexpectedly so BaseTool can turn it into an observation."""

    name = "broken"
    description = "Fail internally"
    parameters_type: ClassVar[type[BaseModel]] = ValueParameters

    def run(self, parameters: ValueParameters) -> ToolExecution:
        del parameters
        raise RuntimeError("private implementation failure")


class ExplodingRegistry(ToolRegistry):
    """Simulate an unexpected failure outside a tool's safety wrapper."""

    def execute(self, call: ToolCall) -> ToolResult:
        del call
        raise RuntimeError("registry infrastructure failed")


class HangingProvider:
    """Never respond unless the runtime timeout cancels it."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        return None


def call(
    call_id: str,
    name: str,
    **arguments: object,
) -> ToolCall:
    """Build one provider-normalized tool call."""
    return ToolCall(
        id=call_id,
        name=name,
        arguments=cast(dict[str, JsonValue], arguments),
    )


def tool_response(*calls: ToolCall, text: str | None = None) -> ModelResponse:
    """Build a response containing optional text and ordered tool calls."""
    parts: list[TextPart | ToolCallPart] = []
    if text is not None:
        parts.append(TextPart(text=text))
    parts.extend(ToolCallPart(tool_call=item) for item in calls)
    return ModelResponse(content=tuple(parts), finish_reason="tool_calls")


def text_response(
    text: str = "Finished",
    *,
    usage: Usage | None = None,
) -> ModelResponse:
    """Build a final response."""
    return ModelResponse(
        content=(TextPart(text=text),),
        usage=usage or Usage(),
        finish_reason="stop",
    )


def run_config(
    workspace: Path,
    *,
    budget: Budget | None = None,
) -> RunConfig:
    """Build a deterministic run configuration."""
    return RunConfig(
        task="Inspect the repository",
        workspace=workspace,
        budget=budget or Budget(),
        run_id=RUN_ID,
    )


@pytest.mark.asyncio
async def test_reads_file_then_completes_with_structured_history(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# ForgeAgent\n", encoding="utf-8")
    provider = FakeModelProvider(
        (
            tool_response(call("call-1", "read_file", path="README.md")),
            text_response("README inspected"),
        )
    )
    registry = ToolRegistry((ReadFileTool(Workspace(tmp_path)),))

    result = await AgentRuntime(provider=provider, tools=registry).run(run_config(tmp_path))

    assert result.status is RunStatus.COMPLETED
    assert result.stop_reason is StopReason.COMPLETED
    assert result.final_text == "README inspected"
    assert result.metrics.steps == 2
    assert result.metrics.tool_calls == 1
    assert [message.role for message in result.messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    tool_message = result.messages[3]
    tool_result = tool_message.content[0].tool_result  # type: ignore[union-attr]
    assert tool_result.ok is True
    assert "# ForgeAgent" in tool_result.output
    assert provider.requests[0].tools[0].name == "read_file"
    assert provider.requests[1].messages == result.messages[:-1]


@pytest.mark.asyncio
async def test_multiple_tool_calls_execute_in_response_order(tmp_path: Path) -> None:
    seen: list[str] = []
    provider = FakeModelProvider(
        (
            tool_response(
                call("call-1", "record", value="first"),
                call("call-2", "record", value="second"),
            ),
            text_response(),
        )
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((RecordingTool(seen),)),
    ).run(run_config(tmp_path))

    assert result.status is RunStatus.COMPLETED
    assert seen == ["first", "second"]
    assert result.metrics.tool_calls == 2


@pytest.mark.parametrize(
    ("requested_call", "error_code"),
    (
        (call("unknown-1", "missing_tool"), "unknown_tool"),
        (call("invalid-1", "record", unexpected=True), "invalid_arguments"),
    ),
)
@pytest.mark.asyncio
async def test_tool_failures_are_observations_then_model_can_finish(
    tmp_path: Path,
    requested_call: ToolCall,
    error_code: str,
) -> None:
    provider = FakeModelProvider((tool_response(requested_call), text_response()))
    registry = ToolRegistry((RecordingTool([]),))

    result = await AgentRuntime(provider=provider, tools=registry).run(run_config(tmp_path))

    assert result.status is RunStatus.COMPLETED
    observed = result.messages[3].content[0].tool_result  # type: ignore[union-attr]
    assert observed.ok is False
    assert observed.metadata["error_code"] == error_code


@pytest.mark.asyncio
async def test_repeated_same_failure_stops_at_configured_threshold(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        tuple(tool_response(call(f"call-{index}", "missing_tool")) for index in range(1, 4))
    )
    budget = Budget(max_repeated_failures=3)

    result = await AgentRuntime(provider=provider, tools=ToolRegistry()).run(
        run_config(tmp_path, budget=budget)
    )

    assert result.status is RunStatus.STOPPED
    assert result.stop_reason is StopReason.REPEATED_FAILURE
    assert result.metrics.steps == 3
    assert result.metrics.tool_calls == 3
    assert result.messages[-1].role is Role.TOOL
    final_observation = result.messages[-1].content[0].tool_result  # type: ignore[union-attr]
    assert final_observation.call_id == "call-3"


@pytest.mark.asyncio
async def test_success_resets_repeated_failure_counter(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        (
            tool_response(call("bad-1", "missing_tool")),
            tool_response(call("good", "record", value="ok")),
            tool_response(call("bad-2", "missing_tool")),
            text_response(),
        )
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((RecordingTool([]),)),
    ).run(run_config(tmp_path, budget=Budget(max_repeated_failures=2)))

    assert result.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_max_steps_stops_before_another_model_request(tmp_path: Path) -> None:
    provider = FakeModelProvider((tool_response(call("call-1", "record", value="once")),))

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((RecordingTool([]),)),
    ).run(run_config(tmp_path, budget=Budget(max_steps=1)))

    assert result.status is RunStatus.STOPPED
    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.metrics.steps == 1
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_max_tool_calls_stops_partway_through_ordered_batch(tmp_path: Path) -> None:
    seen: list[str] = []
    provider = FakeModelProvider(
        (
            tool_response(
                call("call-1", "record", value="first"),
                call("call-2", "record", value="second"),
            ),
        )
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((RecordingTool(seen),)),
    ).run(run_config(tmp_path, budget=Budget(max_tool_calls=1)))

    assert result.stop_reason is StopReason.MAX_TOOL_CALLS
    assert result.metrics.tool_calls == 1
    assert seen == ["first"]


@pytest.mark.asyncio
async def test_token_budget_stops_before_final_text_is_accepted(tmp_path: Path) -> None:
    provider = FakeModelProvider((text_response(usage=Usage(input_tokens=7, output_tokens=3)),))

    result = await AgentRuntime(provider=provider, tools=ToolRegistry()).run(
        run_config(tmp_path, budget=Budget(max_tokens=10))
    )

    assert result.status is RunStatus.STOPPED
    assert result.stop_reason is StopReason.MAX_TOKENS
    assert result.metrics.total_tokens == 10
    assert result.final_text is None


@pytest.mark.asyncio
async def test_reported_cost_budget_is_accumulated_and_stops(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        (
            tool_response(
                call("call-1", "record", value="one"),
            ).model_copy(
                update={"usage": Usage(cost=Decimal("0.40"))},
            ),
            text_response(usage=Usage(cost=Decimal("0.60"))),
        )
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((RecordingTool([]),)),
    ).run(run_config(tmp_path, budget=Budget(max_cost=Decimal("1.00"))))

    assert result.status is RunStatus.STOPPED
    assert result.stop_reason is StopReason.MAX_COST
    assert result.metrics.known_cost == Decimal("1.00")
    assert result.metrics.cost_complete is True


@pytest.mark.asyncio
async def test_missing_provider_cost_is_marked_incomplete(tmp_path: Path) -> None:
    provider = FakeModelProvider((text_response(),))

    result = await AgentRuntime(provider=provider, tools=ToolRegistry()).run(run_config(tmp_path))

    assert result.status is RunStatus.COMPLETED
    assert result.metrics.known_cost == Decimal("0")
    assert result.metrics.cost_complete is False


@pytest.mark.asyncio
async def test_wall_clock_timeout_returns_stopped_result(tmp_path: Path) -> None:
    result = await AgentRuntime(provider=HangingProvider(), tools=ToolRegistry()).run(
        run_config(tmp_path, budget=Budget(timeout_seconds=Decimal("0.01")))
    )

    assert result.status is RunStatus.STOPPED
    assert result.stop_reason is StopReason.TIMEOUT
    assert result.metrics.steps == 0


@pytest.mark.asyncio
async def test_provider_error_returns_failed_result(tmp_path: Path) -> None:
    provider = FakeModelProvider((ProviderResponseError("safe provider failure"),))

    result = await AgentRuntime(provider=provider, tools=ToolRegistry()).run(run_config(tmp_path))

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.PROVIDER_ERROR
    assert result.error_type == "ProviderResponseError"
    assert result.error_message == "safe provider failure"


@pytest.mark.asyncio
async def test_cancelled_provider_returns_cancelled_result(tmp_path: Path) -> None:
    provider = FakeModelProvider((asyncio.CancelledError(),))

    result = await AgentRuntime(provider=provider, tools=ToolRegistry()).run(run_config(tmp_path))

    assert result.status is RunStatus.CANCELLED
    assert result.stop_reason is StopReason.USER_CANCELLED
    assert result.error_type is None


@pytest.mark.asyncio
async def test_base_tool_exception_is_observed_and_loop_continues(tmp_path: Path) -> None:
    provider = FakeModelProvider(
        (
            tool_response(call("broken-1", "broken", value="x")),
            text_response(),
        )
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((BrokenTool(),)),
    ).run(run_config(tmp_path))

    assert result.status is RunStatus.COMPLETED
    observed = result.messages[3].content[0].tool_result  # type: ignore[union-attr]
    assert observed.metadata["error_code"] == "tool_execution_error"
    assert "private implementation failure" not in (observed.error or "")


@pytest.mark.asyncio
async def test_unexpected_registry_exception_is_terminal_tool_error(tmp_path: Path) -> None:
    provider = FakeModelProvider((tool_response(call("call-1", "missing_tool")),))

    result = await AgentRuntime(provider=provider, tools=ExplodingRegistry()).run(
        run_config(tmp_path)
    )

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.TOOL_ERROR
    assert result.error_type == "RuntimeError"
    assert "registry infrastructure failed" not in (result.error_message or "")


@pytest.mark.asyncio
async def test_unexpected_provider_exception_is_internal_error(tmp_path: Path) -> None:
    provider = FakeModelProvider((RuntimeError("private provider detail"),))

    result = await AgentRuntime(provider=provider, tools=ToolRegistry()).run(run_config(tmp_path))

    assert result.status is RunStatus.FAILED
    assert result.stop_reason is StopReason.INTERNAL_ERROR
    assert result.error_message == "RuntimeError during agent execution"


@pytest.mark.asyncio
async def test_trace_events_match_successful_state_transitions(tmp_path: Path) -> None:
    memory = MemoryEventSink()
    tracer = RunTracer(bus=EventBus((memory,)), run_id=RUN_ID)
    provider = FakeModelProvider((text_response("Done"),))

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry(),
        tracer=tracer,
    ).run(run_config(tmp_path))

    assert result.status is RunStatus.COMPLETED
    assert [event.kind for event in memory.events] == [
        EventKind.RUN_STARTED,
        EventKind.MODEL_REQUEST_STARTED,
        EventKind.MODEL_RESPONSE_RECEIVED,
        EventKind.BUDGET_UPDATED,
        EventKind.RUN_COMPLETED,
    ]
    assert tracer.terminal_kind is EventKind.RUN_COMPLETED
    assert memory.events[-1].data["stop_reason"] == "completed"


@pytest.mark.asyncio
async def test_stopped_run_emits_run_stopped_terminal_event(tmp_path: Path) -> None:
    memory = MemoryEventSink()
    tracer = RunTracer(bus=EventBus((memory,)), run_id=RUN_ID)
    provider = FakeModelProvider((tool_response(call("call-1", "record", value="once")),))

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((RecordingTool([]),)),
        tracer=tracer,
    ).run(run_config(tmp_path, budget=Budget(max_steps=1)))

    assert result.stop_reason is StopReason.MAX_STEPS
    assert memory.events[-1].kind is EventKind.RUN_STOPPED
    assert tracer.terminal_kind is EventKind.RUN_STOPPED


def test_tracer_and_config_run_ids_must_match(tmp_path: Path) -> None:
    tracer = RunTracer(bus=EventBus(()))
    runtime = AgentRuntime(
        provider=FakeModelProvider((text_response(),)),
        tools=ToolRegistry(),
        tracer=tracer,
    )

    with pytest.raises(ValueError, match="run_id"):
        asyncio.run(runtime.run(run_config(tmp_path)))
