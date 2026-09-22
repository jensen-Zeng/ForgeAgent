"""Turn-oriented agent engine used by safe runs and persistent RLM sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from forge_agent.domain import Message, RunConfig, RunMetrics, RunStatus, StopReason
from forge_agent.models import ModelProvider
from forge_agent.runtime import AgentRuntime, build_initial_messages
from forge_agent.tools import ToolRegistry
from forge_agent.tracing import EventKind, RunTracer, Visibility

T = TypeVar("T")


class CancellationScope:
    """Cooperative cancellation signal with race-free task cleanup."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason = "cancelled"
        self._boundary_requested = False

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def cancel(self, reason: str = "cancelled") -> None:
        self._reason = reason
        self._event.set()

    @property
    def boundary_requested(self) -> bool:
        return self._boundary_requested

    def cancel_at_boundary(self, reason: str = "steer") -> None:
        self._reason = reason
        self._boundary_requested = True

    async def wait(self) -> None:
        await self._event.wait()

    async def run(self, operation: Awaitable[T]) -> T:
        task = asyncio.ensure_future(operation)
        waiter = asyncio.create_task(self.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done and not task.done():
                task.cancel()
            return await task
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)


class TurnStopReason(StrEnum):
    """Stable turn-level stop reasons, independent from provider SDKs."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR = "tool_error"
    COMPACTION_FAILED = "compaction_failed"
    INTERNAL_ERROR = "internal_error"


class ConversationContext(BaseModel):
    """Persistent messages plus the immutable configuration for one turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: UUID = Field(default_factory=uuid4)
    messages: tuple[Message, ...]
    run_config: RunConfig
    generation: int = Field(default=0, ge=0)

    @classmethod
    def start(cls, config: RunConfig, *, session_id: UUID | None = None) -> ConversationContext:
        return cls(
            session_id=session_id or uuid4(),
            messages=build_initial_messages(config),
            run_config=config,
        )


class TurnResult(BaseModel):
    """Normalized output of exactly one engine turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    status: RunStatus
    stop_reason: TurnStopReason
    run_stop_reason: StopReason
    messages: tuple[Message, ...]
    final_text: str | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    error_type: str | None = None
    error_message: str | None = None


EventSink = Callable[[EventKind, dict[str, object]], None]


class AgentEngine(Protocol):
    """A provider-neutral engine that advances one conversation turn."""

    async def run_turn(
        self,
        context: ConversationContext,
        tools: ToolRegistry,
        event_sink: EventSink | None,
        cancel_scope: CancellationScope,
    ) -> TurnResult: ...


class LoopAgentEngine:
    """Adapter exposing the existing, security-reviewed loop as a turn engine."""

    def __init__(self, provider: ModelProvider, *, tracer: RunTracer | None = None) -> None:
        self._provider = provider
        self._tracer = tracer

    async def run_turn(
        self,
        context: ConversationContext,
        tools: ToolRegistry,
        event_sink: EventSink | None = None,
        cancel_scope: CancellationScope | None = None,
    ) -> TurnResult:
        scope = cancel_scope or CancellationScope()
        turn_id = uuid4()
        self._emit(
            EventKind.TURN_STARTED,
            {"turn_id": str(turn_id), "session_id": str(context.session_id)},
            event_sink,
        )
        runtime = AgentRuntime(
            provider=self._provider,
            tools=tools,
            tracer=self._tracer,
            streaming=True,
            boundary_cancelled=lambda: scope.boundary_requested,
        )
        result = await scope.run(runtime.run(context.run_config, initial_messages=context.messages))
        stop_reason = _turn_reason(result.status, result.stop_reason)
        terminal_kind = (
            EventKind.TURN_CANCELLED
            if result.status is RunStatus.CANCELLED
            else EventKind.TURN_COMPLETED
        )
        self._emit(
            terminal_kind,
            {
                "turn_id": str(turn_id),
                "session_id": str(context.session_id),
                "stop_reason": stop_reason.value,
            },
            event_sink,
            trace=False,
        )
        return TurnResult(
            turn_id=turn_id,
            session_id=context.session_id,
            status=result.status,
            stop_reason=stop_reason,
            run_stop_reason=result.stop_reason,
            messages=result.messages,
            final_text=result.final_text,
            metrics=result.metrics,
            error_type=result.error_type,
            error_message=result.error_message,
        )

    def _emit(
        self,
        kind: EventKind,
        data: dict[str, object],
        sink: EventSink | None,
        *,
        trace: bool = True,
    ) -> None:
        if sink is not None:
            sink(kind, data)
        if trace and self._tracer is not None:
            self._tracer.emit(
                kind,
                data={key: str(value) for key, value in data.items()},
                visibility=Visibility.DEBUG,
            )


def _turn_reason(status: RunStatus, reason: StopReason) -> TurnStopReason:
    if status is RunStatus.CANCELLED:
        return TurnStopReason.CANCELLED
    if reason is StopReason.PROVIDER_ERROR:
        return TurnStopReason.PROVIDER_ERROR
    if reason is StopReason.TOOL_ERROR:
        return TurnStopReason.TOOL_ERROR
    if reason is StopReason.INTERNAL_ERROR:
        return TurnStopReason.INTERNAL_ERROR
    if reason is StopReason.COMPLETED:
        return TurnStopReason.COMPLETED
    return TurnStopReason.LIMIT_REACHED
