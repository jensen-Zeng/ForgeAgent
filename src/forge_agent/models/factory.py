"""Create configured model providers without leaking vendor types."""

from __future__ import annotations

import asyncio

import httpx

from forge_agent.config import ModelSettings
from forge_agent.models.anthropic import AnthropicProvider
from forge_agent.models.base import ModelProvider, Sleep
from forge_agent.models.ollama import OllamaProvider
from forge_agent.models.openai_compatible import OpenAICompatibleProvider
from forge_agent.tracing import RunTracer


def create_model_provider(
    settings: ModelSettings,
    *,
    client: httpx.AsyncClient | None = None,
    sleep: Sleep = asyncio.sleep,
    tracer: RunTracer | None = None,
) -> ModelProvider:
    """Build the selected live provider from validated application settings."""
    settings.require_live_configuration()
    if settings.base_url is None or settings.name is None:
        raise AssertionError("live configuration validation did not detect missing fields")
    if settings.provider == "anthropic":
        if settings.api_key is None:
            raise AssertionError("live configuration validation did not detect a missing API key")
        return AnthropicProvider(
            model_name=settings.name,
            api_key=settings.api_key.get_secret_value(),
            base_url=str(settings.base_url),
            connect_timeout_seconds=float(settings.connect_timeout_seconds),
            read_timeout_seconds=float(settings.read_timeout_seconds),
            total_timeout_seconds=float(settings.total_timeout_seconds),
            max_retries=settings.max_retries,
            max_output_tokens=settings.max_output_tokens,
            client=client,
            sleep=sleep,
            tracer=tracer,
            anthropic_version=settings.anthropic_version,
            input_cost_per_million=settings.input_cost_per_million,
            output_cost_per_million=settings.output_cost_per_million,
            cached_input_cost_per_million=settings.cached_input_cost_per_million,
        )
    provider_type = OllamaProvider if settings.provider == "ollama" else OpenAICompatibleProvider
    return provider_type(
        model_name=settings.name,
        api_key=(
            settings.api_key.get_secret_value() if settings.api_key is not None else "ollama-local"
        ),
        base_url=str(settings.base_url),
        connect_timeout_seconds=float(settings.connect_timeout_seconds),
        read_timeout_seconds=float(settings.read_timeout_seconds),
        total_timeout_seconds=float(settings.total_timeout_seconds),
        max_retries=settings.max_retries,
        max_output_tokens=settings.max_output_tokens,
        client=client,
        sleep=sleep,
        tracer=tracer,
        input_cost_per_million=settings.input_cost_per_million,
        output_cost_per_million=settings.output_cost_per_million,
        cached_input_cost_per_million=settings.cached_input_cost_per_million,
    )
