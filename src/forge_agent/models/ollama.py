"""Ollama's OpenAI-compatible local endpoint."""

from forge_agent.models.openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    """OpenAI-compatible adapter identified separately in traces."""

    provider_name = "ollama"
