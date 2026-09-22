"""Provider wire models and shared message conversion helpers."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from forge_agent.domain import (
    Message,
    Role,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResultPart,
    ToolSchema,
)
from forge_agent.models.base import ProviderResponseError


class WireModel(BaseModel):
    """Permissive base for provider responses that may add new fields."""

    model_config = ConfigDict(extra="ignore")


class OpenAIFunctionCall(WireModel):
    """Function payload inside an OpenAI tool call."""

    name: str
    arguments: str


class OpenAIToolCall(WireModel):
    """OpenAI Chat Completions function tool call."""

    id: str
    type: Literal["function"] = "function"
    function: OpenAIFunctionCall


class OpenAIResponseMessage(WireModel):
    """Assistant message in an OpenAI completion choice."""

    content: str | None = None
    tool_calls: list[OpenAIToolCall] = Field(default_factory=list)


class OpenAIChoice(WireModel):
    """One OpenAI completion candidate."""

    index: int = 0
    finish_reason: str | None = None
    message: OpenAIResponseMessage


class OpenAIPromptDetails(WireModel):
    """Optional cached-token details."""

    cached_tokens: int = Field(default=0, ge=0)


class OpenAIUsage(WireModel):
    """OpenAI usage counters."""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    prompt_tokens_details: OpenAIPromptDetails | None = None


class OpenAIChatResponse(WireModel):
    """Subset of an OpenAI Chat Completions response used by ForgeAgent."""

    id: str | None = None
    choices: list[OpenAIChoice]
    usage: OpenAIUsage | None = None


class AnthropicContentBlock(WireModel):
    """Text or tool-use block in an Anthropic response."""

    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, JsonValue] | None = None


class AnthropicUsage(WireModel):
    """Anthropic Messages usage counters."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)


class AnthropicMessageResponse(WireModel):
    """Subset of an Anthropic Messages response used by ForgeAgent."""

    id: str | None = None
    content: list[AnthropicContentBlock]
    stop_reason: str | None = None
    usage: AnthropicUsage | None = None


def openai_messages(messages: tuple[Message, ...]) -> list[dict[str, JsonValue]]:
    """Convert normalized history to Chat Completions message objects."""
    converted: list[dict[str, JsonValue]] = []
    for message in messages:
        if message.role is Role.TOOL:
            for part in message.content:
                if isinstance(part, ToolResultPart):
                    result = part.tool_result
                    content = result.output if result.ok else result.error or "Tool failed"
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": result.call_id,
                            "content": content,
                        }
                    )
            continue
        text = "\n".join(part.text for part in message.content if isinstance(part, TextPart))
        converted_message: dict[str, JsonValue] = {
            "role": message.role.value,
            "content": text or None,
        }
        tool_calls: list[JsonValue] = []
        for part in message.content:
            if isinstance(part, ToolCallPart):
                call = part.tool_call
                tool_calls.append(
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                )
        if tool_calls:
            converted_message["tool_calls"] = tool_calls
        converted.append(converted_message)
    return converted


def openai_tools(tools: tuple[ToolSchema, ...]) -> list[dict[str, JsonValue]]:
    """Convert tool schemas to OpenAI function definitions."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def anthropic_messages(
    messages: tuple[Message, ...],
) -> tuple[str | None, list[dict[str, JsonValue]]]:
    """Convert history to Anthropic system text and Messages content blocks."""
    system_parts: list[str] = []
    converted: list[dict[str, JsonValue]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            system_parts.extend(part.text for part in message.content if isinstance(part, TextPart))
            continue
        if message.role is Role.TOOL:
            blocks: list[JsonValue] = []
            for part in message.content:
                if isinstance(part, ToolResultPart):
                    result = part.tool_result
                    blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": result.output
                            if result.ok
                            else result.error or "Tool failed",
                            "is_error": not result.ok,
                        }
                    )
            converted.append({"role": "user", "content": blocks})
            continue
        blocks = []
        for part in message.content:
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ToolCallPart):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": part.tool_call.id,
                        "name": part.tool_call.name,
                        "input": part.tool_call.arguments,
                    }
                )
        converted.append({"role": message.role.value, "content": blocks})
    system = "\n".join(system_parts) or None
    return system, converted


def anthropic_tools(tools: tuple[ToolSchema, ...]) -> list[dict[str, JsonValue]]:
    """Convert tool schemas to Anthropic client-tool definitions."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }
        for tool in tools
    ]


def parse_json_arguments(raw_arguments: str, *, call_id: str, name: str) -> ToolCall:
    """Parse and validate model-generated OpenAI function arguments."""
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise ProviderResponseError(
            f"Tool call {call_id!r} returned invalid JSON arguments"
        ) from error
    if not isinstance(parsed, dict):
        raise ProviderResponseError(f"Tool call {call_id!r} arguments must be a JSON object")
    return ToolCall(
        id=call_id,
        name=name,
        arguments=cast(dict[str, Any], parsed),
    )
