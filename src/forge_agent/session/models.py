"""Strict, versioned schemas stored in RLM JSONL journals."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputKind(StrEnum):
    PROMPT = "prompt"
    STEER = "steer"
    FOLLOW_UP = "follow_up"


class InputEnvelope(StrictModel):
    """One durable external input with a stable identity."""

    id: UUID = Field(default_factory=uuid4)
    kind: InputKind = InputKind.PROMPT
    text: str = Field(min_length=1, max_length=1_000_000)
    timestamp: datetime = Field(default_factory=utc_now)
    parent_id: UUID | None = None

    @field_validator("text")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input text cannot be blank")
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class AcceptedInput(StrictModel):
    input_id: UUID
    receipt_sequence: int = Field(gt=0)
    accepted_at: datetime = Field(default_factory=utc_now)
    queued: bool


class SessionHeader(StrictModel):
    """First record of each root or child journal."""

    type: Literal["session_header"] = "session_header"
    version: Literal[1] = 1
    id: UUID = Field(default_factory=uuid4)
    parent_id: UUID | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    session_id: UUID
    root_session_id: UUID
    generation: int = Field(default=0, ge=0)
    workspace: str


class SessionEntry(StrictModel):
    """Forward-compatible journal envelope; unknown types remain in payload."""

    id: UUID = Field(default_factory=uuid4)
    parent_id: UUID | None = None
    timestamp: datetime = Field(default_factory=utc_now)
    type: str = Field(min_length=1, max_length=100)
    session_id: UUID
    root_session_id: UUID
    generation: int = Field(default=0, ge=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class TailInspection(StrictModel):
    valid: bool
    last_good_offset: int = Field(ge=0)
    file_size: int = Field(ge=0)
    error: str | None = None
