"""Anthropic Messages API model provider."""

from __future__ import annotations

import asyncio
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
        """Stage-one compatibility stream; native Anthropic streaming comes later."""
        response = await self.generate(request)
        yield ModelStreamEvent(type=ModelStreamEventType.COMPLETED, response=response)

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
