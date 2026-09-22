"""Model-provider interfaces and adapters."""

from forge_agent.models.anthropic import AnthropicProvider
from forge_agent.models.base import (
    ModelProvider,
    ProviderError,
    ProviderHTTPError,
    ProviderNetworkError,
    ProviderResponseError,
)
from forge_agent.models.factory import create_model_provider
from forge_agent.models.fake import FakeDelay, FakeModelProvider
from forge_agent.models.ollama import OllamaProvider
from forge_agent.models.openai_compatible import OpenAICompatibleProvider
from forge_agent.models.streaming import collect_stream

__all__ = [
    "AnthropicProvider",
    "FakeDelay",
    "FakeModelProvider",
    "ModelProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderHTTPError",
    "ProviderNetworkError",
    "ProviderResponseError",
    "collect_stream",
    "create_model_provider",
]
