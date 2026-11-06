"""Anthropic Messages API model provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import cast

import httpx
from pydantic import JsonValue, ValidationError

from forge_agent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    TextPart,
    ToolCall,
    ToolCallPart,
    Usage,
)
from forge_agent.models.base import (
    HttpModelProvider,
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderResponseError,
    Sleep,
)
from forge_agent.models.wire import (
    AnthropicMessageResponse,
    anthropic_messages,
    anthropic_tools,
)
from forge_agent.tracing import RunTracer


class AnthropicProvider(HttpModelProvider):
    """Adapter for the native Anthropic Messages API."""

    provider_name = "anthropic"

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str,
        anthropic_version: str = "2023-06-01",
        connect_timeout_seconds: float = 10.0,
        read_timeout_seconds: float = 60.0,
        total_timeout_seconds: float = 120.0,
        max_retries: int = 2,
        max_output_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        tracer: RunTracer | None = None,
        input_cost_per_million: Decimal | None = None,
        output_cost_per_million: Decimal | None = None,
        cached_input_cost_per_million: Decimal | None = None,
    ) -> None:
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            total_timeout_seconds=total_timeout_seconds,
            max_retries=max_retries,
            client=client,
            sleep=sleep,
            tracer=tracer,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
            cached_input_cost_per_million=cached_input_cost_per_million,
        )
        self._anthropic_version = anthropic_version
        self._max_output_tokens = max_output_tokens

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Send and normalize one Anthropic Messages request."""
        system, messages = anthropic_messages(request.messages)
        payload: dict[str, JsonValue] = {
            "model": self.model_name,
            "max_tokens": self._max_output_tokens,
            "messages": cast(list[JsonValue], messages),
        }
        if system is not None:
            payload["system"] = system
        if request.tools:
            payload["tools"] = cast(list[JsonValue], anthropic_tools(request.tools))
        response = await self._post_json(
            endpoint="messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._anthropic_version,
                "Content-Type": "application/json",
            },
            payload=payload,
            message_count=len(request.messages),
            tool_count=len(request.tools),
        )
        normalized = self._parse_response(response)
        self._emit_response_received(normalized)
        return normalized

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """Stream native SSE events; streaming requests are not automatically retried."""
        system, messages = anthropic_messages(request.messages)
        payload: dict[str, JsonValue] = {
            "model": self.model_name,
            "max_tokens": self._max_output_tokens,
            "messages": cast(list[JsonValue], messages),
            "stream": True,
        }
        if system is not None:
            payload["system"] = system
        if request.tools:
            payload["tools"] = cast(list[JsonValue], anthropic_tools(request.tools))

        text_chunks: list[str] = []
        blocks: dict[int, dict[str, str]] = {}
        input_tokens = output_tokens = cached_tokens = 0
        finish_reason: str | None = None
        request_id: str | None = None
        saw_start = saw_stop = False
        event_name: str | None = None
        data_lines: list[str] = []
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self._anthropic_version,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        def apply_usage(raw: object) -> None:
            nonlocal input_tokens, output_tokens, cached_tokens
            if not isinstance(raw, dict):
                return
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                value = raw.get(key)
                if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                ):
                    raise ProviderResponseError("Anthropic stream returned invalid usage")
            if isinstance(raw.get("input_tokens"), int):
                input_tokens = raw["input_tokens"]
            if isinstance(raw.get("output_tokens"), int):
                output_tokens = raw["output_tokens"]
            if isinstance(raw.get("cache_read_input_tokens"), int):
                cached_tokens = raw["cache_read_input_tokens"]

        def process_event(name: str, raw_data: str) -> tuple[str, ...] | None:
            nonlocal saw_start, saw_stop, finish_reason, request_id
            try:
                item = json.loads(raw_data)
            except json.JSONDecodeError as error:
                raise ProviderResponseError("invalid Anthropic SSE JSON") from error
            if not isinstance(item, dict):
                raise ProviderResponseError("invalid Anthropic SSE event schema")
            kind = item.get("type", name)
            if not isinstance(kind, str) or kind != name:
                raise ProviderResponseError("Anthropic SSE event type mismatch")
            if kind == "ping":
                return None
            if kind == "error":
                error_payload = item.get("error")
                error_type = error_payload.get("type") if isinstance(error_payload, dict) else None
                safe_type = error_type if isinstance(error_type, str) else "stream_error"
                raise ProviderResponseError(f"Anthropic stream error: {safe_type[:100]}")
            known_events = {
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            }
            if kind in known_events and kind != "message_start" and not saw_start:
                raise ProviderResponseError("Anthropic stream event preceded message_start")
            if kind == "message_start":
                if saw_start:
                    raise ProviderResponseError("Anthropic stream repeated message_start")
                message = item.get("message")
                if not isinstance(message, dict):
                    raise ProviderResponseError("Anthropic message_start missing message")
                saw_start = True
                request_id = message.get("id") if isinstance(message.get("id"), str) else None
                apply_usage(message.get("usage"))
            elif kind == "content_block_start":
                index = item.get("index")
                block = item.get("content_block")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index in blocks
                    or not isinstance(block, dict)
                ):
                    raise ProviderResponseError("invalid Anthropic content block start")
                block_type = block.get("type")
                if block_type == "text":
                    if not isinstance(block.get("text", ""), str):
                        raise ProviderResponseError("invalid Anthropic text block")
                    blocks[index] = {"type": "text"}
                    initial = block.get("text", "")
                    if isinstance(initial, str) and initial:
                        text_chunks.append(initial)
                        return ("text", initial)
                elif block_type == "tool_use":
                    tool_id, tool_name = block.get("id"), block.get("name")
                    if not isinstance(tool_id, str) or not isinstance(tool_name, str):
                        raise ProviderResponseError("Anthropic tool block missing id or name")
                    blocks[index] = {
                        "type": "tool",
                        "id": tool_id,
                        "name": tool_name,
                        "json": "",
                        "initial_json": json.dumps(block.get("input", {})),
                    }
                elif not isinstance(block_type, str):
                    raise ProviderResponseError("invalid Anthropic content block type")
                else:
                    blocks[index] = {"type": str(block_type)}
            elif kind == "content_block_delta":
                index = item.get("index")
                delta = item.get("delta")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or not isinstance(delta, dict)
                    or index not in blocks
                ):
                    raise ProviderResponseError("invalid Anthropic content block delta")
                block = blocks[index]
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    if block["type"] != "text" or not isinstance(delta.get("text"), str):
                        raise ProviderResponseError(
                            "invalid Anthropic text delta for content block"
                        )
                    text = delta["text"]
                    text_chunks.append(text)
                    return ("text", text)
                if delta_type == "input_json_delta":
                    if block["type"] != "tool" or not isinstance(delta.get("partial_json"), str):
                        raise ProviderResponseError(
                            "invalid Anthropic JSON delta for content block"
                        )
                    part = delta["partial_json"]
                    block["json"] = block.get("json", "") + part
                    return ("tool", str(index), part)
            elif kind == "content_block_stop":
                index = item.get("index")
                if (
                    not isinstance(index, int)
                    or isinstance(index, bool)
                    or index < 0
                    or index not in blocks
                ):
                    raise ProviderResponseError("invalid Anthropic content block stop")
            elif kind == "message_delta":
                delta = item.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
                    finish_reason = delta["stop_reason"]
                apply_usage(item.get("usage"))
            elif kind == "message_stop":
                if saw_stop:
                    raise ProviderResponseError("Anthropic stream repeated message_stop")
                saw_stop = True
            # Unknown event types are forward-compatible and ignored.
            return None

        async def dispatch(name: str | None, lines: list[str]) -> AsyncIterator[ModelStreamEvent]:
            if name is None or not lines:
                return
            parsed = process_event(name, "\n".join(lines))
            if parsed is None:
                return
            if parsed[0] == "text":
                yield ModelStreamEvent(type=ModelStreamEventType.TEXT_DELTA, text=parsed[1])
            else:
                index = int(parsed[1])
                block = blocks[index]
                yield ModelStreamEvent(
                    type=ModelStreamEventType.TOOL_ARGUMENT_DELTA,
                    tool_index=index,
                    tool_call_id=block["id"],
                    tool_name=block["name"],
                    arguments_delta=parsed[2],
                )

        self._emit_request_started(
            attempt=1, message_count=len(request.messages), tool_count=len(request.tools)
        )
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/messages",
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                ) as response:
                    if not response.is_success:
                        await response.aread()
                        error_type, message, response_id = self._parse_http_error(response)
                        raise ProviderHTTPError(
                            message,
                            status_code=response.status_code,
                            request_id=response_id,
                            error_type=error_type,
                            retryable=self._is_retryable_status(response.status_code),
                            attempts=1,
                        )
                    request_id = self._request_id_from_headers(response)
                    async for line in response.aiter_lines():
                        if line == "":
                            async for event in dispatch(event_name, data_lines):
                                yield event
                            event_name, data_lines = None, []
                        elif line.startswith("event:"):
                            event_name = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
                    if data_lines:
                        async for event in dispatch(event_name, data_lines):
                            yield event
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise ProviderNetworkError("Anthropic stream timed out", attempts=1) from error
        except httpx.TransportError as error:
            raise ProviderNetworkError(
                f"Anthropic stream failed: {type(error).__name__}", attempts=1
            ) from None

        if not saw_start or not saw_stop:
            raise ProviderResponseError("Anthropic SSE stream ended before message_stop")
        parts: list[TextPart | ToolCallPart] = []
        text = "".join(text_chunks)
        if text:
            parts.append(TextPart(text=text))
        for index in sorted(blocks):
            block = blocks[index]
            if block["type"] == "tool":
                try:
                    arguments = json.loads(block["json"] or block["initial_json"])
                except json.JSONDecodeError as error:
                    raise ProviderResponseError(
                        "Anthropic tool input contained invalid JSON"
                    ) from error
                if not isinstance(arguments, dict):
                    raise ProviderResponseError("Anthropic tool input must be a JSON object")
                parts.append(
                    ToolCallPart(
                        tool_call=ToolCall(id=block["id"], name=block["name"], arguments=arguments)
                    )
                )
        if not parts:
            raise ProviderResponseError("Anthropic stream returned no text or tool calls")
        usage = self._price_usage(
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_tokens,
            ),
            cached_tokens_included_in_input=False,
        )
        normalized = ModelResponse(
            content=tuple(parts),
            usage=usage,
            finish_reason=finish_reason,
            provider_request_id=request_id,
        )
        self._emit_response_received(normalized)
        yield ModelStreamEvent(type=ModelStreamEventType.COMPLETED, response=normalized)

    def _parse_response(self, response: httpx.Response) -> ModelResponse:
        try:
            wire = AnthropicMessageResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise ProviderResponseError(
                "Anthropic API returned an invalid response schema"
            ) from error
        parts: list[TextPart | ToolCallPart] = []
        for block in wire.content:
            if block.type == "text" and block.text is not None:
                parts.append(TextPart(text=block.text))
            elif block.type == "tool_use":
                if block.id is None or block.name is None or block.input is None:
                    raise ProviderResponseError(
                        "Anthropic tool_use block is missing id, name, or input"
                    )
                parts.append(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id=block.id,
                            name=block.name,
                            arguments=block.input,
                        )
                    )
                )
        if not parts:
            raise ProviderResponseError("Anthropic API returned neither text nor tool calls")
        usage = wire.usage
        normalized_usage = Usage()
        if usage is not None:
            normalized_usage = Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cache_read_input_tokens,
            )
            normalized_usage = self._price_usage(
                normalized_usage,
                cached_tokens_included_in_input=False,
            )
        request_id = wire.id or self._request_id_from_headers(response)
        return ModelResponse(
            content=tuple(parts),
            usage=normalized_usage,
            finish_reason=wire.stop_reason,
            provider_request_id=request_id,
        )
