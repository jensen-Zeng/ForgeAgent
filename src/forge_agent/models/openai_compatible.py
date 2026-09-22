"""OpenAI Chat Completions compatible model provider."""

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
    ToolCallPart,
    Usage,
)
from forge_agent.models.base import (
    HttpModelProvider,
    ProviderResponseError,
    Sleep,
)
from forge_agent.models.wire import (
    OpenAIChatResponse,
    openai_messages,
    openai_tools,
    parse_json_arguments,
)
from forge_agent.tracing import RunTracer


class OpenAICompatibleProvider(HttpModelProvider):
    """Adapter for OpenAI and services implementing Chat Completions."""

    provider_name = "openai"

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str,
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
        self._max_output_tokens = max_output_tokens

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Send and normalize one Chat Completions request."""
        payload: dict[str, JsonValue] = {
            "model": self.model_name,
            "messages": cast(list[JsonValue], openai_messages(request.messages)),
            "max_completion_tokens": self._max_output_tokens,
        }
        if request.tools:
            payload["tools"] = cast(list[JsonValue], openai_tools(request.tools))
            payload["tool_choice"] = "auto"
        response = await self._post_json(
            endpoint="chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
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
        """Stream Chat Completions SSE and aggregate fragmented tool arguments."""
        payload: dict[str, JsonValue] = {
            "model": self.model_name,
            "messages": cast(list[JsonValue], openai_messages(request.messages)),
            "max_completion_tokens": self._max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = cast(list[JsonValue], openai_tools(request.tools))
            payload["tool_choice"] = "auto"
        text_chunks: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason: str | None = None
        request_id: str | None = None
        saw_done = False
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self._timeout,
                ) as response:
                    if not response.is_success:
                        await response.aread()
                        error_type, message, response_id = self._parse_http_error(response)
                        from forge_agent.models.base import ProviderHTTPError

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
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            saw_done = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError as error:
                            raise ProviderResponseError("invalid OpenAI SSE JSON") from error
                        if not isinstance(chunk, dict):
                            continue
                        if isinstance(chunk.get("id"), str):
                            request_id = chunk["id"]
                        raw_usage = chunk.get("usage")
                        if isinstance(raw_usage, dict):
                            usage = Usage(
                                input_tokens=int(raw_usage.get("prompt_tokens", 0)),
                                output_tokens=int(raw_usage.get("completion_tokens", 0)),
                                total_tokens=int(raw_usage.get("total_tokens", 0)),
                            )
                            usage = self._price_usage(usage, cached_tokens_included_in_input=True)
                        choices = chunk.get("choices")
                        if not isinstance(choices, list) or not choices:
                            continue
                        choice = choices[0]
                        if not isinstance(choice, dict):
                            continue
                        if isinstance(choice.get("finish_reason"), str):
                            finish_reason = choice["finish_reason"]
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        content = delta.get("content")
                        if isinstance(content, str):
                            text_chunks.append(content)
                            yield ModelStreamEvent(
                                type=ModelStreamEventType.TEXT_DELTA, text=content
                            )
                        tool_calls = delta.get("tool_calls")
                        if isinstance(tool_calls, list):
                            for raw_call in tool_calls:
                                if not isinstance(raw_call, dict):
                                    continue
                                index = int(raw_call.get("index", 0))
                                aggregate = calls.setdefault(
                                    index, {"id": "", "name": "", "arguments": ""}
                                )
                                if isinstance(raw_call.get("id"), str):
                                    aggregate["id"] = raw_call["id"]
                                function = raw_call.get("function")
                                arguments_delta = ""
                                if isinstance(function, dict):
                                    if isinstance(function.get("name"), str):
                                        aggregate["name"] += function["name"]
                                    if isinstance(function.get("arguments"), str):
                                        arguments_delta = function["arguments"]
                                        aggregate["arguments"] += arguments_delta
                                yield ModelStreamEvent(
                                    type=ModelStreamEventType.TOOL_ARGUMENT_DELTA,
                                    tool_index=index,
                                    tool_call_id=aggregate["id"] or None,
                                    tool_name=aggregate["name"] or None,
                                    arguments_delta=arguments_delta,
                                )
        except (TimeoutError, httpx.TransportError) as error:
            from forge_agent.models.base import ProviderNetworkError

            raise ProviderNetworkError(
                f"OpenAI-compatible stream failed: {type(error).__name__}", attempts=1
            ) from None
        if not saw_done:
            raise ProviderResponseError("OpenAI-compatible SSE stream ended before [DONE]")
        parts: list[TextPart | ToolCallPart] = []
        if text_chunks:
            parts.append(TextPart(text="".join(text_chunks)))
        for index in sorted(calls):
            aggregate = calls[index]
            call = parse_json_arguments(
                aggregate["arguments"],
                call_id=aggregate["id"] or f"stream-call-{index}",
                name=aggregate["name"],
            )
            parts.append(ToolCallPart(tool_call=call))
        if not parts:
            raise ProviderResponseError("OpenAI-compatible stream returned no content")
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
            wire = OpenAIChatResponse.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise ProviderResponseError(
                "OpenAI-compatible API returned an invalid response schema"
            ) from error
        if not wire.choices:
            raise ProviderResponseError("OpenAI-compatible API returned no choices")
        choice = wire.choices[0]
        parts: list[TextPart | ToolCallPart] = []
        if choice.message.content:
            parts.append(TextPart(text=choice.message.content))
        for wire_call in choice.message.tool_calls:
            call = parse_json_arguments(
                wire_call.function.arguments,
                call_id=wire_call.id,
                name=wire_call.function.name,
            )
            parts.append(ToolCallPart(tool_call=call))
        if not parts:
            raise ProviderResponseError(
                "OpenAI-compatible API returned neither text nor tool calls"
            )
        usage = wire.usage
        normalized_usage = Usage()
        if usage is not None:
            cached = (
                usage.prompt_tokens_details.cached_tokens
                if usage.prompt_tokens_details is not None
                else 0
            )
            normalized_usage = Usage(
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                cached_input_tokens=cached,
                total_tokens=usage.total_tokens,
            )
            normalized_usage = self._price_usage(
                normalized_usage,
                cached_tokens_included_in_input=True,
            )
        request_id = wire.id or self._request_id_from_headers(response)
        return ModelResponse(
            content=tuple(parts),
            usage=normalized_usage,
            finish_reason=choice.finish_reason,
            provider_request_id=request_id,
        )
