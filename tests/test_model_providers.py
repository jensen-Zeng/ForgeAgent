"""Tests for Fake, OpenAI-compatible, and Anthropic model providers."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx
from pydantic import JsonValue

from forge_agent.config import ConfigurationError, load_settings
from forge_agent.domain import (
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
    ToolSchema,
)
from forge_agent.models import (
    AnthropicProvider,
    FakeModelProvider,
    OllamaProvider,
    OpenAICompatibleProvider,
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderResponseError,
    create_model_provider,
)
from forge_agent.models.base import Sleep
from forge_agent.tracing import EventBus, EventKind, MemoryEventSink, Redactor, RunTracer

OPENAI_URL = "https://openai.example/v1/chat/completions"
ANTHROPIC_URL = "https://anthropic.example/v1/messages"


async def no_sleep(delay: float) -> None:
    """Skip retry delays in deterministic tests."""
    del delay


def user_request(*, tools: tuple[ToolSchema, ...] = ()) -> ModelRequest:
    """Create a minimal normalized request."""
    return ModelRequest(
        messages=(Message(role=Role.USER, content=(TextPart(text="Hello"),)),),
        tools=tools,
    )


def read_tool_schema() -> ToolSchema:
    """Create a small function schema supported by both providers."""
    return ToolSchema(
        name="read_file",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def openai_response(
    *,
    content: str | None = "Hello back",
    tool_calls: list[dict[str, JsonValue]] | None = None,
    usage: dict[str, JsonValue] | None = None,
    finish_reason: str = "stop",
) -> dict[str, JsonValue]:
    """Build an OpenAI-compatible response body."""
    body: dict[str, JsonValue] = {
        "id": "chatcmpl-123",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": cast(list[JsonValue], tool_calls or []),
                },
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def anthropic_response(
    *,
    content: list[dict[str, JsonValue]],
    usage: dict[str, JsonValue] | None = None,
    stop_reason: str = "end_turn",
) -> dict[str, JsonValue]:
    """Build an Anthropic Messages response body."""
    body: dict[str, JsonValue] = {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": cast(list[JsonValue], content),
        "stop_reason": stop_reason,
    }
    if usage is not None:
        body["usage"] = usage
    return body


def make_openai(
    *,
    max_retries: int = 0,
    client: httpx.AsyncClient | None = None,
    tracer: RunTracer | None = None,
    sleep: Sleep = no_sleep,
) -> OpenAICompatibleProvider:
    """Create a test OpenAI provider."""
    return OpenAICompatibleProvider(
        model_name="test-openai-model",
        api_key="test-secret-key",
        base_url="https://openai.example/v1",
        max_retries=max_retries,
        client=client,
        sleep=sleep,
        tracer=tracer,
    )


def make_anthropic(
    *,
    max_retries: int = 0,
    client: httpx.AsyncClient | None = None,
) -> AnthropicProvider:
    """Create a test Anthropic provider."""
    return AnthropicProvider(
        model_name="test-anthropic-model",
        api_key="test-anthropic-key",
        base_url="https://anthropic.example/v1",
        max_retries=max_retries,
        client=client,
        sleep=no_sleep,
    )


@pytest.mark.asyncio
async def test_fake_provider_is_scripted_and_records_requests() -> None:
    expected = ModelResponse(content=(TextPart(text="scripted"),))
    provider = FakeModelProvider((expected,))
    request = user_request()

    actual = await provider.generate(request)
    await provider.aclose()

    assert actual == expected
    assert provider.requests == [request]
    assert provider.closed is True


@pytest.mark.asyncio
async def test_fake_provider_raises_scripted_error_and_exhaustion() -> None:
    provider = FakeModelProvider((RuntimeError("scripted failure"),))

    with pytest.raises(RuntimeError, match="scripted failure"):
        await provider.generate(user_request())
    with pytest.raises(ProviderResponseError, match="exhausted"):
        await provider.generate(user_request())


@respx.mock
@pytest.mark.asyncio
async def test_openai_text_response_usage_and_request_shape() -> None:
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json=openai_response(
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                    "prompt_tokens_details": {"cached_tokens": 3},
                }
            ),
        )
    )
    provider = make_openai()

    response = await provider.generate(user_request(tools=(read_tool_schema(),)))
    await provider.aclose()

    assert response.text_parts[0].text == "Hello back"
    assert response.tool_calls == ()
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 4
    assert response.usage.cached_input_tokens == 3
    assert response.provider_request_id == "chatcmpl-123"
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "test-openai-model"
    assert sent["messages"][0] == {"role": "user", "content": "Hello"}
    assert sent["tools"][0]["function"]["name"] == "read_file"
    assert route.calls[0].request.headers["authorization"] == "Bearer test-secret-key"


@respx.mock
@pytest.mark.asyncio
async def test_openai_multiple_tool_calls_are_normalized_in_order() -> None:
    tool_calls: list[dict[str, JsonValue]] = [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
        },
        {
            "id": "call-2",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"LICENSE"}'},
        },
    ]
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json=openai_response(
                content="I will inspect both files.",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
            ),
        )
    )
    provider = make_openai()

    response = await provider.generate(user_request(tools=(read_tool_schema(),)))
    await provider.aclose()

    assert response.text_parts[0].text.startswith("I will")
    assert [call.id for call in response.tool_calls] == ["call-1", "call-2"]
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.finish_reason == "tool_calls"


@pytest.mark.parametrize(
    "arguments",
    (
        '{"path":',
        '["README.md"]',
    ),
)
@respx.mock
@pytest.mark.asyncio
async def test_openai_invalid_tool_arguments_are_rejected(arguments: str) -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json=openai_response(
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": arguments},
                    }
                ],
            ),
        )
    )
    provider = make_openai()

    with pytest.raises(ProviderResponseError, match="arguments"):
        await provider.generate(user_request())
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_openai_missing_tool_arguments_are_rejected() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json=openai_response(
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read_file"},
                    }
                ],
            ),
        )
    )
    provider = make_openai()

    with pytest.raises(ProviderResponseError, match="schema"):
        await provider.generate(user_request())
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_openai_empty_response_is_rejected() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json=openai_response(content=None))
    )
    provider = make_openai()

    with pytest.raises(ProviderResponseError, match="neither text nor tool calls"):
        await provider.generate(user_request())
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_openai_truncated_json_response_is_rejected_safely() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            text='{"choices":',
            headers={"content-type": "application/json"},
        )
    )
    provider = make_openai()

    with pytest.raises(ProviderResponseError, match="invalid response schema"):
        await provider.generate(user_request())
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_openai_missing_usage_is_safe() -> None:
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=openai_response()))
    provider = make_openai()

    response = await provider.generate(user_request())
    await provider.aclose()

    assert response.usage.total_tokens == 0
    assert response.usage.cost is None


@respx.mock
@pytest.mark.asyncio
async def test_retryable_429_retries_then_succeeds() -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    route = respx.post(OPENAI_URL).mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"retry-after": "0.01"},
                json={"error": {"type": "rate_limit_error", "message": "slow down"}},
            ),
            httpx.Response(200, json=openai_response()),
        ]
    )
    provider = make_openai(max_retries=2, sleep=record_sleep)

    response = await provider.generate(user_request())
    await provider.aclose()

    assert response.text_parts[0].text == "Hello back"
    assert route.call_count == 2
    assert delays == [0.01]


@respx.mock
@pytest.mark.asyncio
async def test_401_fails_without_retry_and_hides_service_message() -> None:
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            401,
            headers={"x-request-id": "req-auth"},
            json={
                "error": {
                    "type": "authentication_error",
                    "message": "echoed test-secret-key",
                }
            },
        )
    )
    provider = make_openai(max_retries=3)

    with pytest.raises(ProviderHTTPError) as captured:
        await provider.generate(user_request())
    await provider.aclose()

    assert captured.value.status_code == 401
    assert captured.value.attempts == 1
    assert captured.value.request_id == "req-auth"
    assert "test-secret-key" not in str(captured.value)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_total_timeout_is_retried_then_exhausted() -> None:
    attempts = 0

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del request
        attempts += 1
        await asyncio.sleep(0.03)
        return httpx.Response(200, json=openai_response())

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow_handler))
    provider = OpenAICompatibleProvider(
        model_name="model",
        api_key="key",
        base_url="https://openai.example/v1",
        total_timeout_seconds=0.005,
        max_retries=1,
        client=client,
        sleep=no_sleep,
    )

    with pytest.raises(ProviderNetworkError) as captured:
        await provider.generate(user_request())
    await provider.aclose()
    await client.aclose()

    assert captured.value.attempts == 2
    assert attempts == 2


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry() -> None:
    attempts = 0

    async def cancelled_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del request
        attempts += 1
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(cancelled_handler))
    provider = make_openai(max_retries=3, client=client)

    with pytest.raises(asyncio.CancelledError):
        await provider.generate(user_request())
    await provider.aclose()
    await client.aclose()

    assert attempts == 1


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_text_tool_usage_headers_and_request_shape() -> None:
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_response(
                content=[
                    {"type": "text", "text": "Reading now."},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "read_file",
                        "input": {"path": "README.md"},
                    },
                ],
                usage={
                    "input_tokens": 20,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 5,
                },
                stop_reason="tool_use",
            ),
        )
    )
    provider = make_anthropic()
    system_message = Message(
        role=Role.SYSTEM,
        content=(TextPart(text="You are a coding agent."),),
    )
    request = ModelRequest(
        messages=(
            system_message,
            Message(role=Role.USER, content=(TextPart(text="Read README"),)),
        ),
        tools=(read_tool_schema(),),
    )

    response = await provider.generate(request)
    await provider.aclose()

    assert response.text_parts[0].text == "Reading now."
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage.input_tokens == 20
    assert response.usage.cached_input_tokens == 5
    assert response.finish_reason == "tool_use"
    sent = json.loads(route.calls[0].request.content)
    assert sent["system"] == "You are a coding agent."
    assert sent["messages"][0]["role"] == "user"
    assert sent["tools"][0]["input_schema"]["type"] == "object"
    assert route.calls[0].request.headers["x-api-key"] == "test-anthropic-key"
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_history_maps_tool_use_and_result_blocks() -> None:
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_response(content=[{"type": "text", "text": "Done"}]),
        )
    )
    provider = make_anthropic()
    tool_call = ToolCall(id="toolu_1", name="read_file", arguments={"path": "README.md"})
    tool_result = ToolResult(
        call_id="toolu_1",
        name="read_file",
        ok=True,
        output="file content",
    )
    request = ModelRequest(
        messages=(
            Message(role=Role.USER, content=(TextPart(text="Read it"),)),
            Message(
                role=Role.ASSISTANT,
                content=(ToolCallPart(tool_call=tool_call),),
            ),
            Message(
                role=Role.TOOL,
                content=(ToolResultPart(tool_result=tool_result),),
            ),
        )
    )

    await provider.generate(request)
    await provider.aclose()

    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][1]["content"][0]["type"] == "tool_use"
    assert sent["messages"][2]["role"] == "user"
    assert sent["messages"][2]["content"][0]["type"] == "tool_result"
    assert sent["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_529_retries() -> None:
    route = respx.post(ANTHROPIC_URL).mock(
        side_effect=[
            httpx.Response(
                529,
                json={"error": {"type": "overloaded_error", "message": "busy"}},
            ),
            httpx.Response(
                200,
                json=anthropic_response(content=[{"type": "text", "text": "Ready"}]),
            ),
        ]
    )
    provider = make_anthropic(max_retries=1)

    response = await provider.generate(user_request())
    await provider.aclose()

    assert response.text_parts[0].text == "Ready"
    assert route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_trace_records_only_bounded_request_metadata() -> None:
    secret = "test-secret-key"
    memory = MemoryEventSink()
    tracer = RunTracer(
        bus=EventBus((memory,)),
        redactor=Redactor(known_secrets=(secret,)),
    )
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=openai_response()))
    provider = make_openai(tracer=tracer)

    await provider.generate(
        ModelRequest(
            messages=(
                Message(
                    role=Role.USER,
                    content=(TextPart(text="private full prompt"),),
                ),
            )
        )
    )
    await provider.aclose()

    assert [event.kind for event in memory.events] == [
        EventKind.MODEL_REQUEST_STARTED,
        EventKind.MODEL_RESPONSE_RECEIVED,
    ]
    serialized = "\n".join(event.model_dump_json() for event in memory.events)
    assert secret not in serialized
    assert "private full prompt" not in serialized
    assert memory.events[0].data["message_count"] == 1


@pytest.mark.asyncio
async def test_provider_factory_uses_user_configuration(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={
            "FORGE_MODEL_PROVIDER": "anthropic",
            "FORGE_MODEL_API_KEY": "key",
            "FORGE_MODEL_BASE_URL": "https://anthropic.example/v1",
            "FORGE_MODEL_NAME": "claude-test",
            "FORGE_MODEL_MAX_RETRIES": "4",
            "FORGE_AGENT_MAX_COST_USD": "2.50",
        },
    )

    provider = create_model_provider(settings.model, sleep=no_sleep)

    assert isinstance(provider, AnthropicProvider)
    assert settings.agent.max_cost_usd == Decimal("2.50")
    await provider.aclose()


def test_provider_factory_requires_complete_live_configuration(tmp_path: Path) -> None:
    settings = load_settings(cwd=tmp_path, environ={})

    with pytest.raises(ConfigurationError, match="FORGE_MODEL_API_KEY"):
        create_model_provider(settings.model, sleep=no_sleep)


@pytest.mark.asyncio
async def test_ollama_factory_needs_no_api_key(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={
            "FORGE_MODEL_PROVIDER": "ollama",
            "FORGE_MODEL_BASE_URL": "http://localhost:11434/v1",
            "FORGE_MODEL_NAME": "qwen2.5-coder:7b",
        },
    )

    settings.model.require_live_configuration()
    provider = create_model_provider(settings.model, sleep=no_sleep)

    assert isinstance(provider, OllamaProvider)
    assert provider.provider_name == "ollama"
    await provider.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_ollama_openai_compatible_response_is_normalized() -> None:
    route = respx.post("http://localhost:11434/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=openai_response(
                content=None,
                tool_calls=[
                    {
                        "id": "local-call",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                        },
                    }
                ],
                finish_reason="tool_calls",
            ),
        )
    )
    provider = OllamaProvider(
        model_name="qwen2.5-coder:7b",
        api_key="ollama-local",
        base_url="http://localhost:11434/v1",
        max_retries=0,
        sleep=no_sleep,
    )

    response = await provider.generate(user_request(tools=(read_tool_schema(),)))
    await provider.aclose()

    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert route.calls[0].request.headers["authorization"] == "Bearer ollama-local"


@respx.mock
@pytest.mark.asyncio
async def test_openai_explicit_pricing_accounts_for_cached_subset() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json=openai_response(
                usage={
                    "prompt_tokens": 1000,
                    "completion_tokens": 500,
                    "total_tokens": 1500,
                    "prompt_tokens_details": {"cached_tokens": 200},
                }
            ),
        )
    )
    provider = OpenAICompatibleProvider(
        model_name="priced-model",
        api_key="key",
        base_url="https://openai.example/v1",
        max_retries=0,
        input_cost_per_million=Decimal("2"),
        output_cost_per_million=Decimal("4"),
        cached_input_cost_per_million=Decimal("1"),
    )

    response = await provider.generate(user_request())
    await provider.aclose()

    assert response.usage.cost == Decimal("0.0038")


@respx.mock
@pytest.mark.asyncio
async def test_anthropic_explicit_pricing_treats_cache_read_as_additional() -> None:
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json=anthropic_response(
                content=[{"type": "text", "text": "priced"}],
                usage={
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_read_input_tokens": 200,
                },
            ),
        )
    )
    provider = AnthropicProvider(
        model_name="priced-model",
        api_key="key",
        base_url="https://anthropic.example/v1",
        max_retries=0,
        input_cost_per_million=Decimal("2"),
        output_cost_per_million=Decimal("4"),
        cached_input_cost_per_million=Decimal("1"),
    )

    response = await provider.generate(user_request())
    await provider.aclose()

    assert response.usage.cost == Decimal("0.0042")


def test_direct_provider_rejects_incomplete_pricing() -> None:
    with pytest.raises(ValueError, match="configured together"):
        OpenAICompatibleProvider(
            model_name="priced-model",
            api_key="key",
            base_url="https://openai.example/v1",
            input_cost_per_million=Decimal("2"),
        )
