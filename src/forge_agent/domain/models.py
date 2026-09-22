"""Core domain models independent of any model-provider SDK."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


class DomainModel(BaseModel):
    """Immutable base model for values crossing module boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(StrEnum):
    """Message roles understood by the ForgeAgent runtime."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolSchema(DomainModel):
    """Provider-independent description of a callable tool."""

    name: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    description: str = Field(min_length=1)
    parameters: dict[str, JsonValue]


class ToolCall(DomainModel):
    """A validated request from a model to invoke a tool."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(DomainModel):
    """Structured outcome returned after executing a tool call."""

    call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    ok: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_error_state(self) -> ToolResult:
        """Keep success and error fields semantically consistent."""
        if self.ok and self.error is not None:
            raise ValueError("a successful tool result cannot contain an error")
        if not self.ok and not self.error:
            raise ValueError("a failed tool result must contain an error")
        return self


class TextPart(DomainModel):
    """Plain text in a message."""

    type: Literal["text"] = "text"
    text: str


class ToolCallPart(DomainModel):
    """Tool call embedded in an assistant message."""

    type: Literal["tool_call"] = "tool_call"
    tool_call: ToolCall


class ToolResultPart(DomainModel):
    """Tool observation embedded in a tool message."""

    type: Literal["tool_result"] = "tool_result"
    tool_result: ToolResult


ContentPart = Annotated[
    TextPart | ToolCallPart | ToolResultPart,
    Field(discriminator="type"),
]
AssistantPart = Annotated[TextPart | ToolCallPart, Field(discriminator="type")]


class Message(DomainModel):
    """A normalized conversation message."""

    role: Role
    content: tuple[ContentPart, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_parts_for_role(self) -> Message:
        """Reject content parts that cannot belong to the selected role."""
        has_tool_call = any(isinstance(part, ToolCallPart) for part in self.content)
        has_tool_result = any(isinstance(part, ToolResultPart) for part in self.content)
        if has_tool_call and self.role is not Role.ASSISTANT:
            raise ValueError("tool calls are only valid in assistant messages")
        if has_tool_result and self.role is not Role.TOOL:
            raise ValueError("tool results are only valid in tool messages")
        if self.role is Role.TOOL and not has_tool_result:
            raise ValueError("tool messages must contain a tool result")
        return self


class Usage(DomainModel):
    """Token and monetary usage reported for one model response."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost: NonNegativeDecimal | None = None
    currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def fill_or_validate_total(self) -> Usage:
        """Derive total tokens and reject internally inconsistent usage."""
        expected_total = self.input_tokens + self.output_tokens
        if self.total_tokens is None:
            object.__setattr__(self, "total_tokens", expected_total)
        elif self.total_tokens < expected_total:
            raise ValueError("total_tokens cannot be lower than input_tokens + output_tokens")
        return self


class ModelRequest(DomainModel):
    """Normalized request sent through a model provider."""

    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolSchema, ...] = ()


class ModelResponse(DomainModel):
    """Normalized model response supporting text and tool calls."""

    content: tuple[AssistantPart, ...] = Field(min_length=1)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    provider_request_id: str | None = None

    @property
    def text_parts(self) -> tuple[TextPart, ...]:
        """Return all textual response parts."""
        return tuple(part for part in self.content if isinstance(part, TextPart))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Return all tool calls in response order."""
        return tuple(part.tool_call for part in self.content if isinstance(part, ToolCallPart))


class ModelStreamEventType(StrEnum):
    TEXT_DELTA = "text_delta"
    TOOL_ARGUMENT_DELTA = "tool_argument_delta"
    COMPLETED = "completed"


class ModelStreamEvent(DomainModel):
    """Provider-neutral incremental event; completed carries the authoritative response."""

    type: ModelStreamEventType
    text: str | None = None
    tool_index: int | None = Field(default=None, ge=0)
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    response: ModelResponse | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ModelStreamEvent:
        if self.type is ModelStreamEventType.TEXT_DELTA and self.text is None:
            raise ValueError("text delta requires text")
        if self.type is ModelStreamEventType.TOOL_ARGUMENT_DELTA and self.tool_index is None:
            raise ValueError("tool argument delta requires tool_index")
        if self.type is ModelStreamEventType.COMPLETED and self.response is None:
            raise ValueError("completed stream event requires response")
        return self


class Budget(DomainModel):
    """Hard limits applied to a single agent run."""

    max_steps: int = Field(default=30, gt=0)
    timeout_seconds: PositiveDecimal = Decimal("900")
    max_tool_calls: int = Field(default=60, gt=0)
    max_repeated_failures: int = Field(default=3, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost: PositiveDecimal | None = Decimal("1.0")
    currency: Literal["USD"] = "USD"


class RunStatus(StrEnum):
    """Lifecycle states for an agent run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StopReason(StrEnum):
    """Machine-readable reasons why an agent run ended."""

    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    MAX_TOOL_CALLS = "max_tool_calls"
    MAX_TOKENS = "max_tokens"
    MAX_COST = "max_cost"
    REPEATED_FAILURE = "repeated_failure"
    NO_PROGRESS = "no_progress"
    TEST_OSCILLATION = "test_oscillation"
    USER_CANCELLED = "user_cancelled"
    USER_DENIED = "user_denied"
    PROVIDER_ERROR = "provider_error"
    TOOL_ERROR = "tool_error"
    COMPACTION_FAILED = "compaction_failed"
    INTERNAL_ERROR = "internal_error"


class RunConfig(DomainModel):
    """Immutable configuration snapshot for one agent run."""

    task: str = Field(min_length=1)
    workspace: Path
    model_name: str | None = None
    budget: Budget = Field(default_factory=Budget)
    run_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("task", "model_name")
    @classmethod
    def reject_blank_strings(cls, value: str | None) -> str | None:
        """Reject whitespace-only task and model names."""
        if value is not None and not value.strip():
            raise ValueError("value cannot be blank")
        return value

    @field_validator("workspace")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        """Normalize the run workspace to an absolute path."""
        return value.expanduser().resolve(strict=False)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps and normalize them to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)


class RunMetrics(DomainModel):
    """Cumulative work and resource consumption for one run."""

    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    known_cost: NonNegativeDecimal = Decimal("0")
    cost_complete: bool = True
    duration_seconds: NonNegativeDecimal = Decimal("0")
    context_estimated_tokens: int = Field(default=0, ge=0)
    context_compressions: int = Field(default=0, ge=0)
    deduplicated_results: int = Field(default=0, ge=0)
    repeated_tool_calls: int = Field(default=0, ge=0)
    currency: Literal["USD"] = "USD"


class PlanStatus(StrEnum):
    """Public progress state without private chain-of-thought."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class PlanStep(DomainModel):
    """One concise, observable task milestone."""

    title: str = Field(min_length=1, max_length=200)
    status: PlanStatus = PlanStatus.PENDING


class TaskPlan(DomainModel):
    """Short structured plan safe to expose in reports and traces."""

    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=10)


class RunResult(DomainModel):
    """Structured terminal outcome returned for every Agent Runtime path."""

    run_id: UUID
    status: RunStatus
    stop_reason: StopReason
    messages: tuple[Message, ...] = Field(min_length=2)
    final_text: str | None = None
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    started_at: datetime
    finished_at: datetime
    error_type: str | None = None
    error_message: str | None = None
    changed_files: tuple[str, ...] = ()
    diff_summary: str | None = None
    plan: TaskPlan | None = None
    handoff_report: str | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def normalize_result_time(cls, value: datetime) -> datetime:
        """Require aware result timestamps and normalize them to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("result timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> RunResult:
        """Keep lifecycle status, stop reason, and errors consistent."""
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot be earlier than started_at")
        if self.status is RunStatus.COMPLETED:
            if self.stop_reason is not StopReason.COMPLETED:
                raise ValueError("completed runs require the completed stop reason")
            if self.final_text is None:
                raise ValueError("completed runs require final_text")
        elif self.status is RunStatus.CANCELLED:
            if self.stop_reason is not StopReason.USER_CANCELLED:
                raise ValueError("cancelled runs require the user_cancelled stop reason")
        elif self.status is RunStatus.FAILED:
            if self.stop_reason not in {
                StopReason.PROVIDER_ERROR,
                StopReason.TOOL_ERROR,
                StopReason.COMPACTION_FAILED,
                StopReason.INTERNAL_ERROR,
            }:
                raise ValueError("failed runs require an error stop reason")
            if self.error_type is None or self.error_message is None:
                raise ValueError("failed runs require error details")
        elif self.status is RunStatus.STOPPED:
            if self.stop_reason in {
                StopReason.COMPLETED,
                StopReason.USER_CANCELLED,
                StopReason.PROVIDER_ERROR,
                StopReason.TOOL_ERROR,
                StopReason.INTERNAL_ERROR,
            }:
                raise ValueError("stopped runs require a non-terminal-error stop reason")
        else:
            raise ValueError("run results must use a terminal status")
        return self
