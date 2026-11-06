"""Missing provider counters must not become a complete cost estimate."""

import json
from decimal import Decimal

import httpx
import pytest

from forge_agent.domain import Message, ModelRequest, Role, TextPart
from forge_agent.models import AnthropicProvider, OpenAICompatibleProvider
from forge_agent.models.streaming import collect_stream


@pytest.mark.parametrize("mode", ["openai", "openai-stream", "anthropic"])
@pytest.mark.parametrize(
    ("counters", "expected_cost"),
    [
        ({}, None),
        ({"input": 7}, None),
        ({"output": 3}, None),
        ({"input": 0, "output": 0}, Decimal("0")),
        ({"input": 7, "output": 3}, Decimal("0.000026")),
    ],
    ids=["empty", "input-only", "output-only", "reported-zero", "complete"],
)
async def test_pricing_requires_both_reported_token_counters(
    mode: str, counters: dict[str, int], expected_cost: Decimal | None
) -> None:
    keys = (
        {"input": "input_tokens", "output": "output_tokens"}
        if mode == "anthropic"
        else {"input": "prompt_tokens", "output": "completion_tokens"}
    )
    usage = {keys[key]: value for key, value in counters.items()}

    def handler(_: httpx.Request) -> httpx.Response:
        if mode == "openai-stream":
            chunks = [
                {"choices": [{"delta": {"content": "hello"}}]},
                {"choices": [], "usage": usage},
            ]
            body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            return httpx.Response(200, text=body + "data: [DONE]\n\n")
        if mode == "anthropic":
            return httpx.Response(
                200, json={"content": [{"type": "text", "text": "hello"}], "usage": usage}
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hello"}}], "usage": usage}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider_class = AnthropicProvider if mode == "anthropic" else OpenAICompatibleProvider
        provider = provider_class(
            model_name="priced-model",
            api_key="test-key",
            base_url="https://provider.example/v1",
            client=client,
            input_cost_per_million=Decimal("2"),
            output_cost_per_million=Decimal("4"),
        )
        request = ModelRequest(messages=(Message(role=Role.USER, content=(TextPart(text="hi"),)),))
        response = (
            await collect_stream(provider, request)
            if mode == "openai-stream"
            else await provider.generate(request)
        )

    assert response.usage.cost == expected_cost
    assert response.usage.input_tokens == counters.get("input", 0)
    assert response.usage.output_tokens == counters.get("output", 0)
