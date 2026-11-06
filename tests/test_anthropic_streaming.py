"""Offline protocol tests for native Anthropic streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from forge_agent.domain import (
    Message,
    ModelRequest,
    ModelStreamEventType,
    Role,
    TextPart,
    ToolSchema,
)
from forge_agent.models.anthropic import AnthropicProvider
from forge_agent.models.base import ProviderResponseError

URL = "https://anthropic.example/v1/messages"


def frame(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def provider() -> AnthropicProvider:
    return AnthropicProvider(
        model_name="test-model",
        api_key="secret",
        base_url="https://anthropic.example/v1",
        max_retries=0,
        total_timeout_seconds=3,
    )


def request() -> ModelRequest:
    return ModelRequest(
        messages=(Message(role=Role.USER, content=(TextPart(text="hello"),)),),
        tools=(ToolSchema(name="lookup", description="lookup", parameters={"type": "object"}),),
    )


@pytest.mark.asyncio
async def test_stream_aggregates_text_tool_json_and_usage() -> None:
    body = "".join(
        (
            frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_1",
                        "usage": {"input_tokens": 12, "cache_read_input_tokens": 4},
                    },
                },
            ),
            frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            frame("ping", {"type": "ping"}),
            frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hi "},
                },
            ),
            frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "there"},
                },
            ),
            frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
            frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "lookup",
                        "input": {},
                    },
                },
            ),
            frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"key":'},
                },
            ),
            frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '"value"}'},
                },
            ),
            frame("content_block_stop", {"type": "content_block_stop", "index": 1}),
            frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 9},
                },
            ),
            frame("message_stop", {"type": "message_stop"}),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as client:
        p = provider()
        p._client = client
        events = [event async for event in p.stream(request())]
    assert [event.text for event in events if event.type is ModelStreamEventType.TEXT_DELTA] == [
        "Hi ",
        "there",
    ]
    final = events[-1].response
    assert final is not None and final.provider_request_id == "msg_1"
    assert "".join(part.text for part in final.text_parts) == "Hi there"
    assert final.tool_calls[0].arguments == {"key": "value"}
    assert final.usage.input_tokens == 12 and final.usage.output_tokens == 9
    assert final.usage.cached_input_tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        frame("message_start", {"type": "message_start", "message": {"id": "x", "usage": {}}}),
        "event: message_start\ndata: not-json\n\n",
        frame("message_start", {"type": "message_start", "message": {"id": "x", "usage": {}}})
        + frame("error", {"type": "error", "error": {"type": "overloaded_error"}}),
    ],
)
async def test_stream_rejects_truncated_malformed_or_error_events(body: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as client:
        p = provider()
        p._client = client
        with pytest.raises(ProviderResponseError):
            _ = [event async for event in p.stream(request())]


@pytest.mark.asyncio
async def test_tool_block_uses_initial_input_when_no_json_deltas() -> None:
    body = "".join(
        (
            frame("message_start", {"type": "message_start", "message": {"id": "x", "usage": {}}}),
            frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-0",
                        "name": "lookup",
                        "input": {"seed": 7},
                    },
                },
            ),
            frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
            frame("message_stop", {"type": "message_stop"}),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as client:
        p = provider()
        p._client = client
        events = [event async for event in p.stream(request())]
    assert events[-1].response is not None
    assert events[-1].response.tool_calls[0].arguments == {"seed": 7}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_type", "event"),
    [
        (
            "text",
            frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": "{}",
                    },
                },
            ),
        ),
        (
            "tool_use",
            frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": "wrong block type",
                    },
                },
            ),
        ),
    ],
)
async def test_stream_rejects_delta_for_mismatched_block(block_type: str, event: str) -> None:
    body = "".join(
        (
            frame("message_start", {"type": "message_start", "message": {"id": "x", "usage": {}}}),
            frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": (
                        {"type": "text", "text": ""}
                        if block_type == "text"
                        else {"type": "tool_use", "id": "t", "name": "lookup", "input": {}}
                    ),
                },
            ),
            event,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as client:
        p = provider()
        p._client = client
        with pytest.raises(ProviderResponseError):
            _ = [item async for item in p.stream(request())]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [-1, True, "12"])
async def test_stream_rejects_invalid_usage_counts(value: object) -> None:
    body = frame(
        "message_start",
        {"type": "message_start", "message": {"id": "x", "usage": {"input_tokens": value}}},
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as client:
        p = provider()
        p._client = client
        with pytest.raises(ProviderResponseError, match="invalid usage"):
            _ = [item async for item in p.stream(request())]


@pytest.mark.asyncio
async def test_stream_request_enables_streaming() -> None:
    seen: list[dict[str, object]] = []

    async def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(
            200,
            text=frame(
                "message_start", {"type": "message_start", "message": {"id": "x", "usage": {}}}
            )
            + frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            + frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
            )
            + frame("content_block_stop", {"type": "content_block_stop", "index": 0})
            + frame("message_stop", {"type": "message_stop"}),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = provider()
        p._client = client
        _ = [event async for event in p.stream(request())]
    assert seen[0]["stream"] is True


@pytest.mark.asyncio
async def test_cancelled_stream_closes_http_response() -> None:
    closed = False

    class PausingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield frame(
                "message_start",
                {"type": "message_start", "message": {"id": "x", "usage": {}}},
            ).encode()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=PausingStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        p = provider()
        p._client = client
        task = asyncio.create_task(_consume(p))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed


async def _consume(p: AnthropicProvider) -> None:
    async for _ in p.stream(request()):
        pass
