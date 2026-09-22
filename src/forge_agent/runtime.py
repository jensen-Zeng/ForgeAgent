"""Explicit, bounded, and provider-independent Agent Runtime."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from pydantic import JsonValue

from forge_agent.context import ContextManager
from forge_agent.domain import (
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunConfig,
    RunMetrics,
    RunResult,
    RunStatus,
    StopReason,
    TextPart,
    ToolCall,
    ToolResult,
    ToolResultPart,
)
from forge_agent.models import ModelProvider, ProviderError, collect_stream
from forge_agent.prompts import DEFAULT_SYSTEM_PROMPT
from forge_agent.tools import ToolRegistry
from forge_agent.tracing import EventKind, RunTracer, Visibility

MonotonicClock = Callable[[], float]
WallClock = Callable[[], datetime]


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


def build_initial_messages(config: RunConfig) -> tuple[Message, Message]:
    """Build the stable system and user messages for a new run."""
    system = Message(
        role=Role.SYSTEM,
        content=(TextPart(text=DEFAULT_SYSTEM_PROMPT.strip()),),
    )
    user = Message(
        role=Role.USER,
        content=(TextPart(text=config.task),),
    )
    return system, user


class AgentRuntime:
    """Run a deterministic model/tool loop until one explicit terminal reason."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tools: ToolRegistry,
        tracer: RunTracer | None = None,
        context_manager: ContextManager | None = None,
        monotonic: MonotonicClock = time.monotonic,
        wall_clock: WallClock = utc_now,
        streaming: bool = False,
        boundary_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._tracer = tracer
        self._context = context_manager
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._streaming = streaming
        self._boundary_cancelled = boundary_cancelled or (lambda: False)

    async def run(
        self,
        config: RunConfig,
        *,
        initial_messages: tuple[Message, ...] | None = None,
    ) -> RunResult:
        """Execute one run and convert every expected ending into RunResult."""
        if self._tracer is not None and self._tracer.run_id != config.run_id:
            raise ValueError("tracer run_id must match RunConfig.run_id")

        started_at = self._wall_clock()
        started_monotonic = self._monotonic()
        starting_messages = initial_messages
        if starting_messages is None:
            starting_messages = (
                self._context.start(config)
                if self._context is not None
                else build_initial_messages(config)
            )
        state = _RunState(messages=list(starting_messages))
        self._emit(
            EventKind.RUN_STARTED,
            summary="Agent run started.",
            data={
                "max_steps": config.budget.max_steps,
                "max_tool_calls": config.budget.max_tool_calls,
                "max_repeated_failures": config.budget.max_repeated_failures,
                "max_tokens": config.budget.max_tokens,
                "max_cost": (
                    str(config.budget.max_cost) if config.budget.max_cost is not None else None
                ),
                "timeout_seconds": str(config.budget.timeout_seconds),
            },
            visibility=Visibility.USER,
        )

        try:
            async with asyncio.timeout(float(config.budget.timeout_seconds)):
                return await self._run_loop(
                    config,
                    state,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )
        except TimeoutError:
            return self._finish(
                config,
                state,
                status=RunStatus.STOPPED,
                reason=StopReason.TIMEOUT,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        except (asyncio.CancelledError, KeyboardInterrupt):
            return self._finish(
                config,
                state,
                status=RunStatus.CANCELLED,
                reason=StopReason.USER_CANCELLED,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        except Exception as error:
            return self._finish(
                config,
                state,
                status=RunStatus.FAILED,
                reason=StopReason.INTERNAL_ERROR,
                started_at=started_at,
                started_monotonic=started_monotonic,
                error=error,
            )

    async def _run_loop(
        self,
        config: RunConfig,
        state: _RunState,
        *,
        started_at: datetime,
        started_monotonic: float,
    ) -> RunResult:
        while True:
            if state.steps >= config.budget.max_steps:
                return self._finish(
                    config,
                    state,
                    status=RunStatus.STOPPED,
                    reason=StopReason.MAX_STEPS,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )

            next_step = state.steps + 1
            self._emit(
                EventKind.MODEL_REQUEST_STARTED,
                summary=f"Step {next_step}: calling model.",
                data={
                    "step": next_step,
                    "message_count": len(state.messages),
                    "tool_count": len(self._tools.schemas),
                },
                visibility=Visibility.USER,
            )
            try:
                request = ModelRequest(
                    messages=tuple(state.messages),
                    tools=self._tools.schemas,
                )
                response = (
                    await collect_stream(
                        self._provider,
                        request,
                        observer=self._observe_stream,
                    )
                    if self._streaming
                    else await self._provider.generate(request)
                )
            except asyncio.CancelledError:
                raise
            except ProviderError as error:
                return self._finish(
                    config,
                    state,
                    status=RunStatus.FAILED,
                    reason=StopReason.PROVIDER_ERROR,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    error=error,
                )

            state.steps = next_step
            state.record_usage(response)
            state.messages.append(Message(role=Role.ASSISTANT, content=response.content))
            self._emit(
                EventKind.MODEL_RESPONSE_RECEIVED,
                data={
                    "step": state.steps,
                    "finish_reason": response.finish_reason,
                    "text_parts": len(response.text_parts),
                    "tool_calls": len(response.tool_calls),
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.total_tokens,
                    "cost": (str(response.usage.cost) if response.usage.cost is not None else None),
                    "request_id": response.provider_request_id,
                },
            )
            self._emit_budget(config, state)

            budget_reason = self._usage_budget_reason(config, state)
            if budget_reason is not None:
                return self._finish(
                    config,
                    state,
                    status=RunStatus.STOPPED,
                    reason=budget_reason,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )

            if not response.tool_calls:
                final_text = "\n".join(part.text for part in response.text_parts)
                if self._context is not None:
                    self._context.complete_plan()
                return self._finish(
                    config,
                    state,
                    status=RunStatus.COMPLETED,
                    reason=StopReason.COMPLETED,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                    final_text=final_text,
                )

            tool_results: list[ToolResultPart] = []
            for call in response.tool_calls:
                if self._boundary_cancelled():
                    cancelled = tuple(
                        ToolResultPart(
                            tool_result=ToolResult(
                                call_id=pending.id,
                                name=pending.name,
                                ok=False,
                                error="Tool call cancelled before execution.",
                                metadata={"error_code": "turn_steered"},
                            )
                        )
                        for pending in response.tool_calls
                    )
                    state.messages.append(Message(role=Role.TOOL, content=cancelled))
                    return self._finish(
                        config,
                        state,
                        status=RunStatus.CANCELLED,
                        reason=StopReason.USER_CANCELLED,
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                    )
                if state.tool_calls >= config.budget.max_tool_calls:
                    if tool_results:
                        state.messages.append(Message(role=Role.TOOL, content=tuple(tool_results)))
                    return self._finish(
                        config,
                        state,
                        status=RunStatus.STOPPED,
                        reason=StopReason.MAX_TOOL_CALLS,
                        started_at=started_at,
                        started_monotonic=started_monotonic,
                    )
                terminal = await self._execute_tool(
                    config,
                    state,
                    call,
                    tool_results,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )
                if terminal is not None:
                    if tool_results:
                        state.messages.append(Message(role=Role.TOOL, content=tuple(tool_results)))
                    return terminal
            state.messages.append(Message(role=Role.TOOL, content=tuple(tool_results)))
            if self._context is not None:
                state.messages = self._context.optimize(
                    state.messages,
                    self._tools.schemas,
                )

    def _observe_stream(self, event: object) -> None:
        from forge_agent.domain import ModelStreamEvent, ModelStreamEventType

        if not isinstance(event, ModelStreamEvent) or self._tracer is None:
            return
        if event.type is ModelStreamEventType.COMPLETED:
            return
        self._emit(
            EventKind.MODEL_DELTA,
            data={
                "type": event.type.value,
                "text": event.text,
                "tool_index": event.tool_index,
                "tool_call_id": event.tool_call_id,
                "tool_name": event.tool_name,
                "arguments_delta": event.arguments_delta,
            },
        )

    async def _execute_tool(
        self,
        config: RunConfig,
        state: _RunState,
        call: ToolCall,
        tool_results: list[ToolResultPart],
        *,
        started_at: datetime,
        started_monotonic: float,
    ) -> RunResult | None:
        self._emit(
            EventKind.TOOL_CALL_REQUESTED,
            summary=f"Calling tool {call.name}.",
            data={
                "call_id": call.id,
                "tool": call.name,
                "argument_names": cast(list[JsonValue], sorted(call.arguments)),
                "arguments": call.arguments,
            },
            visibility=Visibility.USER,
        )
        if self._context is not None:
            context_reason = self._context.before_tool(call)
            if context_reason is not None:
                self._append_pending_tool_results(state, tool_results)
                return self._finish(
                    config,
                    state,
                    status=RunStatus.STOPPED,
                    reason=context_reason,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )
        try:
            result = await self._tools.execute_async(call)
        except Exception as error:
            self._append_pending_tool_results(state, tool_results)
            return self._finish(
                config,
                state,
                status=RunStatus.FAILED,
                reason=StopReason.TOOL_ERROR,
                started_at=started_at,
                started_monotonic=started_monotonic,
                error=error,
            )

        if self._context is not None:
            result = self._context.process_result(call, result)
        state.tool_calls += 1
        tool_results.append(ToolResultPart(tool_result=result))
        if result.ok and call.name == "apply_patch":
            state.record_patch(result)
        error_code = result.metadata.get("error_code")
        self._emit(
            EventKind.TOOL_CALL_COMPLETED,
            summary=(
                f"Tool {call.name} completed."
                if result.ok
                else f"Tool {call.name} returned an error."
            ),
            data={
                "call_id": call.id,
                "tool": call.name,
                "ok": result.ok,
                "error_code": error_code if isinstance(error_code, str) else None,
                "output": result.output if result.ok else None,
                "error": result.error,
                "metadata": result.metadata,
            },
            visibility=Visibility.USER,
        )
        self._emit_budget(config, state)

        if self._context is not None:
            context_reason = self._context.after_tool(call, result)
            if context_reason is not None:
                self._append_pending_tool_results(state, tool_results)
                return self._finish(
                    config,
                    state,
                    status=RunStatus.STOPPED,
                    reason=context_reason,
                    started_at=started_at,
                    started_monotonic=started_monotonic,
                )

        if not result.ok and error_code == "approval_denied":
            self._append_pending_tool_results(state, tool_results)
            return self._finish(
                config,
                state,
                status=RunStatus.STOPPED,
                reason=StopReason.USER_DENIED,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )

        if result.ok:
            state.last_failure_fingerprint = None
            state.repeated_failures = 0
            return None

        fingerprint = self._failure_fingerprint(call, result)
        if fingerprint == state.last_failure_fingerprint:
            state.repeated_failures += 1
        else:
            state.last_failure_fingerprint = fingerprint
            state.repeated_failures = 1
        if state.repeated_failures >= config.budget.max_repeated_failures:
            self._append_pending_tool_results(state, tool_results)
            return self._finish(
                config,
                state,
                status=RunStatus.STOPPED,
                reason=StopReason.REPEATED_FAILURE,
                started_at=started_at,
                started_monotonic=started_monotonic,
            )
        return None

    def _append_pending_tool_results(
        self,
        state: _RunState,
        tool_results: list[ToolResultPart],
    ) -> None:
        if tool_results:
            state.messages.append(Message(role=Role.TOOL, content=tuple(tool_results)))
            tool_results.clear()

    def _usage_budget_reason(
        self,
        config: RunConfig,
        state: _RunState,
    ) -> StopReason | None:
        if config.budget.max_tokens is not None and state.total_tokens >= config.budget.max_tokens:
            return StopReason.MAX_TOKENS
        if config.budget.max_cost is not None and state.known_cost >= config.budget.max_cost:
            return StopReason.MAX_COST
        return None

    def _failure_fingerprint(self, call: ToolCall, result: ToolResult) -> str:
        error_code = result.metadata.get("error_code")
        stable_code = error_code if isinstance(error_code, str) else "unknown_error"
        return f"{call.name}:{stable_code}"

    def _emit_budget(self, config: RunConfig, state: _RunState) -> None:
        self._emit(
            EventKind.BUDGET_UPDATED,
            data={
                "steps": state.steps,
                "max_steps": config.budget.max_steps,
                "tool_calls": state.tool_calls,
                "max_tool_calls": config.budget.max_tool_calls,
                "total_tokens": state.total_tokens,
                "max_tokens": config.budget.max_tokens,
                "known_cost": str(state.known_cost),
                "max_cost": (
                    str(config.budget.max_cost) if config.budget.max_cost is not None else None
                ),
                "cost_complete": state.cost_complete,
            },
        )

    def _finish(
        self,
        config: RunConfig,
        state: _RunState,
        *,
        status: RunStatus,
        reason: StopReason,
        started_at: datetime,
        started_monotonic: float,
        final_text: str | None = None,
        error: BaseException | None = None,
    ) -> RunResult:
        finished_at = self._wall_clock()
        duration = Decimal(str(max(self._monotonic() - started_monotonic, 0.0)))
        error_type = type(error).__name__ if error is not None else None
        error_message = (
            str(error)
            if isinstance(error, ProviderError)
            else f"{error_type} during agent execution"
            if error is not None
            else None
        )
        context_snapshot = self._context.snapshot() if self._context is not None else None
        recent_errors = tuple(
            part.tool_result.error
            for message in state.messages
            if message.role is Role.TOOL
            for part in message.content
            if isinstance(part, ToolResultPart) and part.tool_result.error is not None
        )
        changed_files = tuple(sorted(state.changed_files))
        handoff_report = (
            self._context.handoff(
                task=config.task,
                status=status.value,
                reason=reason.value,
                changed_files=changed_files,
                recent_errors=recent_errors,
            )
            if self._context is not None and status is not RunStatus.COMPLETED
            else None
        )
        result = RunResult(
            run_id=config.run_id,
            status=status,
            stop_reason=reason,
            messages=tuple(state.messages),
            final_text=final_text,
            metrics=RunMetrics(
                steps=state.steps,
                tool_calls=state.tool_calls,
                input_tokens=state.input_tokens,
                output_tokens=state.output_tokens,
                cached_input_tokens=state.cached_input_tokens,
                total_tokens=state.total_tokens,
                known_cost=state.known_cost,
                cost_complete=state.cost_complete,
                duration_seconds=duration,
                context_estimated_tokens=(
                    context_snapshot.estimated_tokens if context_snapshot is not None else 0
                ),
                context_compressions=(
                    context_snapshot.compressions if context_snapshot is not None else 0
                ),
                deduplicated_results=(
                    context_snapshot.deduplicated_results if context_snapshot is not None else 0
                ),
                repeated_tool_calls=(
                    context_snapshot.repeated_tool_calls if context_snapshot is not None else 0
                ),
            ),
            started_at=started_at,
            finished_at=finished_at,
            error_type=error_type,
            error_message=error_message,
            changed_files=changed_files,
            diff_summary="\n".join(state.patch_diffs) or None,
            plan=self._context.plan if self._context is not None else None,
            handoff_report=handoff_report,
        )
        terminal_kind = {
            RunStatus.COMPLETED: EventKind.RUN_COMPLETED,
            RunStatus.STOPPED: EventKind.RUN_STOPPED,
            RunStatus.FAILED: EventKind.RUN_FAILED,
            RunStatus.CANCELLED: EventKind.RUN_CANCELLED,
        }[status]
        self._emit(
            terminal_kind,
            summary=f"Agent run {status.value}: {reason.value}.",
            data={
                "status": status.value,
                "stop_reason": reason.value,
                "steps": state.steps,
                "tool_calls": state.tool_calls,
                "total_tokens": state.total_tokens,
                "known_cost": str(state.known_cost),
                "cost_complete": state.cost_complete,
                "error_type": error_type,
                "final_text": final_text,
            },
            visibility=Visibility.USER,
        )
        return result

    def _emit(
        self,
        kind: EventKind,
        *,
        summary: str | None = None,
        data: dict[str, JsonValue] | None = None,
        visibility: Visibility = Visibility.DEBUG,
    ) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                kind,
                summary=summary,
                data=data,
                visibility=visibility,
            )


class _RunState:
    """Mutable counters kept private inside one runtime invocation."""

    def __init__(self, *, messages: list[Message]) -> None:
        self.messages = messages
        self.steps = 0
        self.tool_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0
        self.total_tokens = 0
        self.known_cost = Decimal("0")
        self.cost_complete = True
        self.last_failure_fingerprint: str | None = None
        self.repeated_failures = 0
        self.changed_files: set[str] = set()
        self.patch_diffs: list[str] = []

    def record_usage(self, response: ModelResponse) -> None:
        """Accumulate one response's provider-normalized usage."""
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cached_input_tokens += usage.cached_input_tokens
        self.total_tokens += usage.total_tokens or 0
        if usage.cost is None:
            self.cost_complete = False
        else:
            self.known_cost += usage.cost

    def record_patch(self, result: ToolResult) -> None:
        """Capture verified patch evidence for the final run report."""
        changed = result.metadata.get("changed_files")
        if isinstance(changed, list):
            self.changed_files.update(item for item in changed if isinstance(item, str))
        if result.output:
            self.patch_diffs.append(result.output)
