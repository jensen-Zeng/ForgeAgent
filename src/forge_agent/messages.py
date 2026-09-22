"""Durable, bounded, exactly-once parent/child agent mailboxes."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge_agent.session import InputEnvelope, InputKind, SessionRuntime, SessionStore


class AgentMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    root_session_id: UUID
    sender_id: UUID
    target_id: UUID
    text: str = Field(min_length=1, max_length=32_000)
    hop: int = Field(default=0, ge=0, le=8)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message cannot be blank")
        return value


class AgentMessageRouter:
    """Route within one root family; sender identity is host-bound."""

    def __init__(
        self,
        *,
        sender_store: SessionStore,
        stores: dict[UUID, SessionStore],
        runtimes: dict[UUID, SessionRuntime] | None = None,
        per_minute: int = 60,
        max_pending: int = 100,
        max_total: int = 1_000,
    ) -> None:
        self.sender_store = sender_store
        self.stores = stores
        self.runtimes = runtimes or {}
        self.per_minute = per_minute
        self.max_pending = max_pending
        self.max_total = max_total
        self._recent: deque[datetime] = deque()
        self._ids: set[UUID] = set()

    def send(
        self, target_id: UUID, text: str, *, message_id: UUID | None = None, hop: int = 0
    ) -> AgentMessage:
        target = self.stores.get(target_id)
        if target is None:
            raise ValueError("message_target_not_found")
        if target.header.root_session_id != self.sender_store.header.root_session_id:
            raise ValueError("cross_family_message_denied")
        identifier = message_id or uuid4()
        persisted_messages = [
            AgentMessage.model_validate(entry.payload)
            for store in self.stores.values()
            for entry in store.replay()
            if entry.type == "agent_message"
        ]
        if identifier in self._ids or identifier in {message.id for message in persisted_messages}:
            raise ValueError("duplicate_message")
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=1)
        while self._recent and self._recent[0] < cutoff:
            self._recent.popleft()
        persisted_recent = sum(
            message.sender_id == self.sender_store.header.session_id and message.timestamp >= cutoff
            for message in persisted_messages
        )
        if persisted_recent >= self.per_minute:
            raise ValueError("message_rate_exceeded")
        if len(self.list_pending(target_id)) >= self.max_pending:
            raise ValueError("mailbox_full")
        total = len(persisted_messages)
        if total >= self.max_total:
            raise ValueError("family_message_limit_exceeded")
        message = AgentMessage(
            id=identifier,
            root_session_id=self.sender_store.header.root_session_id,
            sender_id=self.sender_store.header.session_id,
            target_id=target_id,
            text=text,
            hop=hop,
            timestamp=now,
        )
        target.append(
            "agent_message",
            message.model_dump(mode="json"),
            entry_id=message.id,
            parent_id=message.sender_id,
        )
        self._recent.append(now)
        self._ids.add(identifier)
        return message

    def list_pending(self, target_id: UUID) -> tuple[AgentMessage, ...]:
        target = self.stores.get(target_id)
        if target is None:
            raise ValueError("message_target_not_found")
        messages: list[AgentMessage] = []
        consumed: set[UUID] = set()
        for entry in target.replay():
            if entry.type == "agent_message":
                messages.append(AgentMessage.model_validate(entry.payload))
            elif entry.type == "agent_message_consumed":
                raw = entry.payload.get("message_id")
                if isinstance(raw, str):
                    consumed.add(UUID(raw))
        return tuple(message for message in messages if message.id not in consumed)

    def acknowledge(self, target_id: UUID, message_id: UUID) -> None:
        target = self.stores.get(target_id)
        if target is None:
            raise ValueError("message_target_not_found")
        if message_id not in {message.id for message in self.list_pending(target_id)}:
            raise ValueError("message_not_pending")
        target.append(
            "agent_message_consumed",
            {"message_id": str(message_id)},
            parent_id=message_id,
        )

    async def deliver_pending(self, target_id: UUID) -> int:
        runtime = self.runtimes.get(target_id)
        if runtime is None:
            raise ValueError("target_runtime_unavailable")
        delivered = 0
        for message in self.list_pending(target_id):
            await runtime.submit(
                InputEnvelope(
                    id=message.id,
                    kind=InputKind.FOLLOW_UP,
                    text=message.text,
                    parent_id=message.sender_id,
                )
            )
            self.acknowledge(target_id, message.id)
            delivered += 1
        return delivered
