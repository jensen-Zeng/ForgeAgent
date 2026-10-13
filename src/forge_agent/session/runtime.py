"""Single-writer authoritative runtime and durable input queue."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from forge_agent.compaction import CompactionFailedError, ContextBuilder
from forge_agent.domain import (
    Message,
    Role,
    RunConfig,
    RunMetrics,
    RunStatus,
    StopReason,
    TextPart,
)
from forge_agent.engine import (
    AgentEngine,
    CancellationScope,
    ConversationContext,
    TurnResult,
    TurnStopReason,
)
from forge_agent.session.models import AcceptedInput, InputEnvelope, InputKind
from forge_agent.session.store import SessionStore
from forge_agent.tools import ToolRegistry
from forge_agent.tracing import EventKind


class SessionRuntime:
    """Serialize turns, persist inputs before ack, and recover unconsumed work."""

    def __init__(
        self,
        *,
        store: SessionStore,
        engine: AgentEngine,
        tools: ToolRegistry,
        base_config: RunConfig,
        context_builder: ContextBuilder | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.tools = tools
        self.base_config = base_config
        self.context_builder = context_builder
        self._queue: asyncio.PriorityQueue[tuple[int, InputEnvelope]] = asyncio.PriorityQueue()
        self._submit_lock = asyncio.Lock()
        self._next_sequence = 0
        self._busy = False
        self._drain_cancelled = False
        self._scope: CancellationScope | None = None
        self._active_envelope: InputEnvelope | None = None
        self._steer_originals: dict[UUID, str] = {}
        self._messages: tuple[Message, ...] = ()
        self._last_result: TurnResult | None = None
        self._leaf_id: UUID | None = None
        self._recover()

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        engine: AgentEngine,
        tools: ToolRegistry,
        base_config: RunConfig,
        context_builder: ContextBuilder | None = None,
    ) -> SessionRuntime:
        return cls(
            store=SessionStore.create(workspace),
            engine=engine,
            tools=tools,
            base_config=base_config,
            context_builder=context_builder,
        )

    @property
    def session_id(self) -> UUID:
        return self.store.header.session_id

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def last_result(self) -> TurnResult | None:
        return self._last_result

    async def submit(self, envelope: InputEnvelope) -> AcceptedInput:
        """Durably append and fsync before reporting acceptance."""
        async with self._submit_lock:
            self._next_sequence += 1
            sequence = self._next_sequence
            self.store.append(
                "input",
                {
                    "envelope": envelope.model_dump(mode="json"),
                    "receipt_sequence": sequence,
                },
                entry_id=envelope.id,
                parent_id=envelope.parent_id,
            )
            await self._queue.put((sequence, envelope))
            if envelope.kind is InputKind.STEER and self._busy and self._scope is not None:
                if self._active_envelope is not None:
                    self._steer_originals[envelope.id] = self._active_envelope.text
                self._scope.cancel_at_boundary("steer")
            return AcceptedInput(
                input_id=envelope.id,
                receipt_sequence=sequence,
                queued=self._busy or sequence > 1,
            )

    async def run_until_idle(self) -> tuple[TurnResult, ...]:
        results: list[TurnResult] = []
        self._drain_cancelled = False
        while not self._drain_cancelled and not self._queue.empty():
            sequence, envelope = await self._queue.get()
            self._busy = True
            self._scope = CancellationScope()
            self._active_envelope = envelope
            try:
                self.store.append(
                    "input_started",
                    {"input_id": str(envelope.id), "receipt_sequence": sequence},
                    parent_id=envelope.id,
                )
                result = await self._run_envelope(envelope)
                self._last_result = result
                results.append(result)
                self.store.append(
                    "input_consumed",
                    {"input_id": str(envelope.id), "receipt_sequence": sequence},
                    parent_id=envelope.id,
                )
                self.store.append(
                    "turn_result",
                    result.model_dump(mode="json"),
                    parent_id=envelope.id,
                )
                common_prefix = 0
                for previous, current in zip(self._messages, result.messages, strict=False):
                    if previous != current:
                        break
                    common_prefix += 1
                for message in result.messages[common_prefix:]:
                    entry = self.store.append(
                        "message",
                        {"message": message.model_dump(mode="json")},
                        parent_id=self._leaf_id,
                    )
                    self._leaf_id = entry.id
                self._messages = result.messages
            finally:
                self._scope = None
                self._active_envelope = None
                self._busy = False
                self._queue.task_done()
        return tuple(results)

    def cancel(self, reason: str = "user_cancelled") -> None:
        self._drain_cancelled = True
        if self._scope is not None:
            self._scope.cancel(reason)

    async def _run_envelope(self, envelope: InputEnvelope) -> TurnResult:
        task_text = envelope.text
        original = self._steer_originals.pop(envelope.id, None)
        if original is not None:
            task_text = f"Original task:\n{original}\n\nSteering update:\n{envelope.text}"
        config = self.base_config.model_copy(update={"task": task_text, "run_id": envelope.id})
        if self._messages:
            messages = (
                *self._messages,
                Message(role=Role.USER, content=(TextPart(text=task_text),)),
            )
        else:
            context = ConversationContext.start(config, session_id=self.session_id)
            messages = context.messages
        if self.context_builder is not None:
            try:
                messages = await self.context_builder.prepare(messages)
            except CompactionFailedError as error:
                return TurnResult(
                    session_id=self.session_id,
                    status=RunStatus.FAILED,
                    stop_reason=TurnStopReason.COMPACTION_FAILED,
                    run_stop_reason=StopReason.COMPACTION_FAILED,
                    messages=messages,
                    metrics=RunMetrics(),
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
        context = ConversationContext(
            session_id=self.session_id,
            messages=messages,
            run_config=config,
            generation=self.store.header.generation,
        )
        return await self.engine.run_turn(
            context,
            self.tools,
            self._event_sink,
            self._scope or CancellationScope(),
        )

    def _event_sink(self, kind: EventKind, data: dict[str, object]) -> None:
        self.store.append("runtime_event", {"kind": kind.value, "data": data})

    def _recover(self) -> None:
        inputs: list[tuple[int, InputEnvelope]] = []
        consumed: set[UUID] = set()
        started: set[UUID] = set()
        last_result: TurnResult | None = None
        for entry in self.store.replay():
            if entry.type == "input":
                raw = entry.payload.get("envelope")
                sequence = entry.payload.get("receipt_sequence")
                if isinstance(raw, dict) and isinstance(sequence, int):
                    inputs.append((sequence, InputEnvelope.model_validate(raw)))
                    self._next_sequence = max(self._next_sequence, sequence)
            elif entry.type == "input_consumed":
                raw_id = entry.payload.get("input_id")
                if isinstance(raw_id, str):
                    consumed.add(UUID(raw_id))
            elif entry.type == "input_started":
                raw_id = entry.payload.get("input_id")
                if isinstance(raw_id, str):
                    started.add(UUID(raw_id))
            elif entry.type == "turn_result":
                last_result = TurnResult.model_validate(entry.payload)
            elif entry.type == "message":
                self._leaf_id = entry.id
        if last_result is not None:
            self._last_result = last_result
            self._messages = last_result.messages
        for sequence, envelope in sorted(inputs, key=lambda item: item[0]):
            if envelope.id not in consumed and envelope.id not in started:
                self._queue.put_nowait((sequence, envelope))
        uncertain = started - consumed
        for input_id in sorted(uncertain, key=str):
            self.store.append(
                "operation_interrupted",
                {"input_id": str(input_id), "state": "uncertain", "replayed": False},
                parent_id=input_id,
            )
        genuinely_pending = [
            envelope
            for _, envelope in inputs
            if envelope.id not in consumed and envelope.id not in started
        ]
        if genuinely_pending or uncertain:
            self.store.append(
                "recovery_marker",
                {
                    "unconsumed_inputs": len(genuinely_pending),
                    "uncertain_inputs": len(uncertain),
                },
            )
