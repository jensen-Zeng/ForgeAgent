"""Stream collection shared by providers and the agent runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from forge_agent.domain import ModelRequest, ModelResponse, ModelStreamEvent, ModelStreamEventType
from forge_agent.models.base import ProviderResponseError

StreamObserver = Callable[[ModelStreamEvent], None]


class StreamProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...


async def collect_stream(
    provider: StreamProvider,
    request: ModelRequest,
    *,
    observer: StreamObserver | None = None,
) -> ModelResponse:
    stream = getattr(provider, "stream", None)
    if stream is None:
        return await provider.generate(request)
    response: ModelResponse | None = None
    async for event in stream(request):
        if observer is not None:
            observer(event)
        if event.type is ModelStreamEventType.COMPLETED:
            response = event.response
    if response is None:
        raise ProviderResponseError("provider stream ended without a completed event")
    return response
