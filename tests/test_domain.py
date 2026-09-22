"""Tests for provider-independent ForgeAgent domain models."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge_agent.domain import (
    Budget,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunConfig,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
    ToolSchema,
    Usage,
)


def make_tool_call() -> ToolCall:
    """Create a reusable valid tool call."""
    return ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})


def test_model_response_supports_text_only() -> None:
    response = ModelResponse(content=(TextPart(text="Done"),))

    assert tuple(part.text for part in response.text_parts) == ("Done",)
    assert response.tool_calls == ()


def test_model_response_supports_tool_call_only() -> None:
    call = make_tool_call()
    response = ModelResponse(content=(ToolCallPart(tool_call=call),))

    assert response.text_parts == ()
    assert response.tool_calls == (call,)


def test_model_response_preserves_mixed_content_order() -> None:
    call = make_tool_call()
    response = ModelResponse(
        content=(
            TextPart(text="I will inspect the file."),
            ToolCallPart(tool_call=call),
        )
    )

    assert response.text_parts[0].text.startswith("I will")
    assert response.tool_calls == (call,)


def test_message_round_trip_uses_discriminated_parts() -> None:
    message = Message(
        role=Role.ASSISTANT,
        content=(TextPart(text="Checking"), ToolCallPart(tool_call=make_tool_call())),
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored == message
    assert isinstance(restored.content[1], ToolCallPart)


def test_tool_result_message_is_valid() -> None:
    result = ToolResult(call_id="call-1", name="read_file", ok=True, output="content")
    message = Message(
        role=Role.TOOL,
        content=(ToolResultPart(tool_result=result),),
    )

    assert isinstance(message.content[0], ToolResultPart)
    assert message.content[0].tool_result.output == "content"


def test_tool_parts_are_restricted_to_matching_roles() -> None:
    with pytest.raises(ValidationError, match="only valid in assistant"):
        Message(
            role=Role.USER,
            content=(ToolCallPart(tool_call=make_tool_call()),),
        )


def test_failed_tool_result_requires_error() -> None:
    with pytest.raises(ValidationError, match="must contain an error"):
        ToolResult(call_id="call-1", name="read_file", ok=False)


def test_successful_tool_result_rejects_error() -> None:
    with pytest.raises(ValidationError, match="cannot contain an error"):
        ToolResult(call_id="call-1", name="read_file", ok=True, error="unexpected")


def test_usage_derives_total_tokens() -> None:
    usage = Usage(input_tokens=10, output_tokens=4, cost=Decimal("0.0012"))

    assert usage.total_tokens == 14
    assert usage.currency == "USD"


def test_usage_rejects_inconsistent_total() -> None:
    with pytest.raises(ValidationError, match="total_tokens"):
        Usage(input_tokens=10, output_tokens=4, total_tokens=13)


def test_request_and_schema_reject_unknown_fields() -> None:
    schema = ToolSchema(
        name="read_file",
        description="Read a file",
        parameters={"type": "object"},
    )
    message = Message(role=Role.USER, content=(TextPart(text="Read README"),))

    request = ModelRequest(messages=(message,), tools=(schema,))

    assert request.tools[0].name == "read_file"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ToolCall.model_validate(
            {"id": "1", "name": "read_file", "arguments": {}, "vendor_field": True}
        )


def test_budget_rejects_non_positive_values() -> None:
    with pytest.raises(ValidationError):
        Budget(max_steps=0)
    with pytest.raises(ValidationError):
        Budget(timeout_seconds=Decimal("-1"))
    with pytest.raises(ValidationError):
        Budget(max_cost=Decimal("0"))
    with pytest.raises(ValidationError):
        Budget(max_repeated_failures=0)


def test_run_config_normalizes_time_and_workspace(tmp_path: Path) -> None:
    east_eight = timezone(timedelta(hours=8))
    created_at = datetime(2026, 7, 26, 12, 0, tzinfo=east_eight)

    config = RunConfig(task="Inspect project", workspace=tmp_path, created_at=created_at)

    assert config.workspace == tmp_path.resolve()
    assert config.created_at == datetime(2026, 7, 26, 4, 0, tzinfo=UTC)


def test_run_config_rejects_naive_time() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        RunConfig(
            task="Inspect project",
            workspace=Path.cwd(),
            created_at=datetime(2026, 7, 26, 12, 0),
        )
