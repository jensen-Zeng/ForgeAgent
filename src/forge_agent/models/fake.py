"""Deterministic scripted model provider for tests and offline development."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass

from forge_agent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
)
from forge_agent.models.base import ProviderResponseError
from forge_agent.models.streaming import collect_stream


@dataclass(frozen=True)
class FakeDelay:
    seconds: float


class FakeModelProvider:
    """Return scripted responses or raise scripted errors in order."""

    def __init__(
        self,
        script: Iterable[ModelResponse | ModelStreamEvent | FakeDelay | BaseException],
    ) -> None:
        self._script = deque(script)
        self.requests: list[ModelRequest] = []
        self.closed = False

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Record a request and consume the next scripted outcome."""
        if self.closed:
            raise ProviderResponseError("FakeModelProvider is closed")
        self.requests.append(request)
        await asyncio.sleep(0)
        if not self._script:
            raise ProviderResponseError("FakeModelProvider script is exhausted")
        outcome = self._script[0] if self._script else None
        if isinstance(outcome, ModelResponse):
            self._script.popleft()
            return outcome
        # Stream scripts are collected without recording the request twice.
        self.requests.pop()
        return await collect_stream(self, request)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        if self.closed:
            raise ProviderResponseError("FakeModelProvider is closed")
        self.requests.append(request)
        while self._script:
            outcome = self._script.popleft()
            if isinstance(outcome, FakeDelay):
                await asyncio.sleep(outcome.seconds)
                continue
            if isinstance(outcome, BaseException):
                raise outcome
            if isinstance(outcome, ModelResponse):
                yield ModelStreamEvent(type=ModelStreamEventType.COMPLETED, response=outcome)
                return
            yield outcome
            if outcome.type is ModelStreamEventType.COMPLETED:
                return
        raise ProviderResponseError("FakeModelProvider script is exhausted")

    async def aclose(self) -> None:
        """Mark the provider closed without external resources."""
        self.closed = True
