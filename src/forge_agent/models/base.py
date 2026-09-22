"""Provider protocol, safe errors, retries, timeouts, and trace metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Protocol, cast

import httpx
from pydantic import JsonValue

from forge_agent.domain import ModelRequest, ModelResponse, Usage
from forge_agent.tracing import EventKind, RunTracer

Sleep = Callable[[float], Awaitable[None]]
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 529})


class ModelProvider(Protocol):
    """Vendor-neutral asynchronous model interface consumed by M05."""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Generate one normalized response."""

    async def aclose(self) -> None:
        """Release provider-owned resources."""


class ProviderError(RuntimeError):
    """Base class for safe model-provider failures."""


class ProviderNetworkError(ProviderError):
    """Raised after retryable transport failures are exhausted."""

    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class ProviderHTTPError(ProviderError):
    """Non-success HTTP response with bounded, non-secret metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        request_id: str | None,
        error_type: str | None,
        retryable: bool,
        attempts: int,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request_id = request_id
        self.error_type = error_type
        self.retryable = retryable
        self.attempts = attempts


class ProviderResponseError(ProviderError):
    """Successful HTTP response that violates the expected provider schema."""


class HttpModelProvider:
    """Shared bounded HTTP behavior for concrete model adapters."""

    provider_name: str

    def __init__(
        self,
        *,
        model_name: str,
        api_key: str,
        base_url: str,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        total_timeout_seconds: float,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        tracer: RunTracer | None = None,
        input_cost_per_million: Decimal | None = None,
        output_cost_per_million: Decimal | None = None,
        cached_input_cost_per_million: Decimal | None = None,
    ) -> None:
        if (input_cost_per_million is None) != (output_cost_per_million is None):
            raise ValueError("input and output pricing must be configured together")
        if cached_input_cost_per_million is not None and input_cost_per_million is None:
            raise ValueError("cached input pricing requires input and output pricing")
        self.model_name = model_name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._total_timeout_seconds = total_timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._tracer = tracer
        self._input_cost_per_million = input_cost_per_million
        self._output_cost_per_million = output_cost_per_million
        self._cached_input_cost_per_million = cached_input_cost_per_million
        self._timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    def _price_usage(
        self,
        usage: Usage,
        *,
        cached_tokens_included_in_input: bool,
    ) -> Usage:
        """Attach USD cost when the user supplied an explicit pricing snapshot."""
        if self._input_cost_per_million is None or self._output_cost_per_million is None:
            return usage
        cached_tokens = usage.cached_input_tokens
        regular_input_tokens = usage.input_tokens
        if cached_tokens_included_in_input:
            regular_input_tokens = max(usage.input_tokens - cached_tokens, 0)
        cached_rate = (
            self._cached_input_cost_per_million
            if self._cached_input_cost_per_million is not None
            else self._input_cost_per_million
        )
        million = Decimal(1_000_000)
        cost = (
            Decimal(regular_input_tokens) * self._input_cost_per_million
            + Decimal(cached_tokens) * cached_rate
            + Decimal(usage.output_tokens) * self._output_cost_per_million
        ) / million
        return usage.model_copy(update={"cost": cost})

    async def aclose(self) -> None:
        """Close only clients created by this provider."""
        if self._owns_client:
            await self._client.aclose()

    async def _post_json(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        payload: Mapping[str, JsonValue],
        message_count: int,
        tool_count: int,
    ) -> httpx.Response:
        last_transport_error: httpx.TransportError | None = None
        for attempt_index in range(self._max_retries + 1):
            attempt = attempt_index + 1
            self._emit_request_started(
                attempt=attempt,
                message_count=message_count,
                tool_count=tool_count,
            )
            try:
                async with asyncio.timeout(self._total_timeout_seconds):
                    response = await self._client.post(
                        f"{self._base_url}/{endpoint.lstrip('/')}",
                        headers=dict(headers),
                        json=dict(payload),
                        timeout=self._timeout,
                    )
            except asyncio.CancelledError:
                raise
            except (TimeoutError, httpx.TransportError) as error:
                if isinstance(error, httpx.TransportError):
                    last_transport_error = error
                if attempt_index >= self._max_retries:
                    self._emit_request_failed(
                        attempt=attempt,
                        error_type=type(error).__name__,
                    )
                    raise ProviderNetworkError(
                        f"{self.provider_name} request failed after {attempt} attempt(s): "
                        f"{type(error).__name__}",
                        attempts=attempt,
                    ) from None
                await self._sleep(self._backoff_delay(attempt_index, None))
                continue

            if response.is_success:
                return response

            retryable = self._is_retryable_status(response.status_code)
            if retryable and attempt_index < self._max_retries:
                await self._sleep(self._backoff_delay(attempt_index, response))
                continue
            error_type, message, request_id = self._parse_http_error(response)
            self._emit_request_failed(
                attempt=attempt,
                status_code=response.status_code,
                request_id=request_id,
                error_type=error_type,
            )
            raise ProviderHTTPError(
                message,
                status_code=response.status_code,
                request_id=request_id,
                error_type=error_type,
                retryable=retryable,
                attempts=attempt,
            )

        raise ProviderNetworkError(
            f"{self.provider_name} request failed: {type(last_transport_error).__name__}",
            attempts=self._max_retries + 1,
        )

    def _is_retryable_status(self, status_code: int) -> bool:
        return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500

    def _backoff_delay(
        self,
        attempt_index: int,
        response: httpx.Response | None,
    ) -> float:
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after is not None:
                try:
                    return min(max(float(retry_after), 0.0), 30.0)
                except ValueError:
                    pass
        return min(0.25 * (2.0**attempt_index), 5.0)

    def _parse_http_error(
        self,
        response: httpx.Response,
    ) -> tuple[str | None, str, str | None]:
        request_id = self._request_id_from_headers(response)
        error_type: str | None = None
        message = f"{self.provider_name} API returned HTTP {response.status_code}"
        try:
            body = response.json()
        except ValueError:
            return error_type, message, request_id
        if isinstance(body, dict):
            raw_request_id = body.get("request_id")
            if request_id is None and isinstance(raw_request_id, str):
                request_id = raw_request_id
            error = body.get("error")
            if isinstance(error, dict):
                raw_type = error.get("type") or error.get("code")
                if isinstance(raw_type, str):
                    error_type = raw_type[:100]
        return error_type, message, request_id

    def _request_id_from_headers(self, response: httpx.Response) -> str | None:
        return cast(
            str | None,
            response.headers.get("x-request-id") or response.headers.get("request-id"),
        )

    def _emit_request_started(
        self,
        *,
        attempt: int,
        message_count: int,
        tool_count: int,
    ) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                EventKind.MODEL_REQUEST_STARTED,
                data={
                    "provider": self.provider_name,
                    "model": self.model_name,
                    "attempt": attempt,
                    "message_count": message_count,
                    "tool_count": tool_count,
                },
            )

    def _emit_response_received(self, response: ModelResponse) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                EventKind.MODEL_RESPONSE_RECEIVED,
                data={
                    "provider": self.provider_name,
                    "model": self.model_name,
                    "request_id": response.provider_request_id,
                    "finish_reason": response.finish_reason,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cached_input_tokens": response.usage.cached_input_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
            )

    def _emit_request_failed(
        self,
        *,
        attempt: int,
        error_type: str | None,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                EventKind.MODEL_REQUEST_FAILED,
                data={
                    "provider": self.provider_name,
                    "model": self.model_name,
                    "attempt": attempt,
                    "status_code": status_code,
                    "request_id": request_id,
                    "error_type": error_type,
                },
            )
