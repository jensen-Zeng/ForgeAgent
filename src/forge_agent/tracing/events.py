"""Versioned event models emitted during an agent run."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


class EventKind(StrEnum):
    """Stable names for observable run lifecycle events."""

    RUN_STARTED = "run_started"
    MODEL_REQUEST_STARTED = "model_request_started"
    MODEL_RESPONSE_RECEIVED = "model_response_received"
    MODEL_REQUEST_FAILED = "model_request_failed"
    TOOL_CALL_REQUESTED = "tool_call_requested"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"
    BUDGET_UPDATED = "budget_updated"
    RUN_COMPLETED = "run_completed"
    RUN_STOPPED = "run_stopped"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TURN_CANCELLED = "turn_cancelled"
    MODEL_DELTA = "model_delta"
    KERNEL_STARTED = "kernel_started"
    KERNEL_EXECUTION_STARTED = "kernel_execution_started"
    KERNEL_EXECUTION_COMPLETED = "kernel_execution_completed"
    KERNEL_INTERRUPTED = "kernel_interrupted"
    KERNEL_RESTARTED = "kernel_restarted"
    CHILD_STATE_CHANGED = "child_state_changed"
    AGENT_MESSAGE_SENT = "agent_message_sent"
    AGENT_MESSAGE_CONSUMED = "agent_message_consumed"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_COMPLETED = "compaction_completed"
    COMPACTION_FAILED = "compaction_failed"
    RECOVERY_RECORDED = "recovery_recorded"


class Visibility(StrEnum):
    """Whether an event summary is suitable for direct user display."""

    USER = "user"
    DEBUG = "debug"


class TraceEvent(BaseModel):
    """One sanitized and JSON-serializable event in a run trace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    session_id: UUID | None = None
    generation: int = Field(default=0, ge=0)
    correlation_id: str | None = None
    sequence: int = Field(gt=0)
    timestamp: datetime
    kind: EventKind
    visibility: Visibility = Visibility.DEBUG
    summary: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict)
    truncated: bool = False

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        """Require an aware timestamp and normalize it to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware")
        return value.astimezone(UTC)
