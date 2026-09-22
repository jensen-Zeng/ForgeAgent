"""Strict request dispatcher whose identity is bound by the owning kernel."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

HostHandler = Callable[[dict[str, JsonValue]], Awaitable[dict[str, JsonValue]]]


class HostRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    operation: str = Field(min_length=1, max_length=100)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class HostResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    ok: bool
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = None
    error: str | None = None


class HostBridge:
    """Dispatch requests arriving from one authenticated Kernel Comm.

    KernelManager owns the Jupyter Comm/control-channel transport. Payload
    session identifiers are neither accepted nor used: handlers are closures
    bound to the owning SessionRuntime. ``directory`` is only a stable audit
    namespace and contains no credential or transport handle.
    """

    def __init__(self, directory: Path, *, timeout_seconds: float = 10.0) -> None:
        self.directory = directory
        self.timeout_seconds = timeout_seconds
        self._handlers: dict[str, HostHandler] = {}

    def register(self, operation: str, handler: HostHandler) -> None:
        if operation in self._handlers:
            raise ValueError(f"host operation already registered: {operation}")
        self._handlers[operation] = handler

    async def close(self) -> None:
        """No transport resource is owned; Comm/control channels belong to KernelManager."""

    async def dispatch(self, request: HostRequest) -> HostResponse:
        handler = self._handlers.get(request.operation)
        if handler is None:
            return HostResponse(
                id=request.id,
                ok=False,
                error_code="unknown_host_request",
                error=f"unsupported host request: {request.operation}",
            )
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await handler(request.payload)
        except TimeoutError:
            return HostResponse(
                id=request.id,
                ok=False,
                error_code="host_request_timeout",
                error="host request timed out",
            )
        except Exception as error:
            return HostResponse(
                id=request.id,
                ok=False,
                error_code="host_request_failed",
                error=type(error).__name__,
            )
        return HostResponse(id=request.id, ok=True, result=result)
