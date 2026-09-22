"""Recursive secret redaction and bounded trace payloads."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from pydantic import JsonValue

REDACTED: Final = "<redacted>"
TRUNCATION_MARKER: Final = "\n...[truncated]"

_SENSITIVE_KEYS: Final = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "x_api_key",
    }
)
_NON_SECRET_TOKEN_FIELDS: Final = frozenset(
    {
        "cached_input_tokens",
        "input_tokens",
        "max_tokens",
        "output_tokens",
        "total_tokens",
    }
)
_SECRET_PATTERNS: Final = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)
_AUTHORIZATION_PATTERN: Final = re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?([^\s,;]+)")


class Redactor:
    """Remove known and commonly formatted secrets from trace values."""

    def __init__(
        self,
        *,
        known_secrets: Iterable[str] = (),
        max_string_chars: int = 8_000,
    ) -> None:
        if max_string_chars <= 0:
            raise ValueError("max_string_chars must be greater than zero")
        self._known_secrets = tuple(
            sorted(
                {secret for secret in known_secrets if secret},
                key=len,
                reverse=True,
            )
        )
        self._max_string_chars = max_string_chars

    def sanitize_text(self, value: str) -> tuple[str, bool]:
        """Redact secrets and truncate one text value."""
        sanitized = value
        for secret in self._known_secrets:
            sanitized = sanitized.replace(secret, REDACTED)
        sanitized = _AUTHORIZATION_PATTERN.sub(rf"\1{REDACTED}", sanitized)
        for pattern in _SECRET_PATTERNS:
            sanitized = pattern.sub(REDACTED, sanitized)
        if len(sanitized) <= self._max_string_chars:
            return sanitized, False
        return sanitized[: self._max_string_chars] + TRUNCATION_MARKER, True

    def sanitize_mapping(
        self,
        value: Mapping[str, JsonValue],
    ) -> tuple[dict[str, JsonValue], bool]:
        """Recursively sanitize a JSON-compatible mapping."""
        sanitized, truncated = self._sanitize_value(dict(value), key=None)
        if not isinstance(sanitized, dict):
            raise TypeError("sanitized mapping unexpectedly changed type")
        return sanitized, truncated

    def _sanitize_value(
        self,
        value: JsonValue,
        *,
        key: str | None,
    ) -> tuple[JsonValue, bool]:
        if key is not None and _is_sensitive_key(key):
            return REDACTED, False
        if isinstance(value, str):
            return self.sanitize_text(value)
        if isinstance(value, list):
            result: list[JsonValue] = []
            was_truncated = False
            for item in value:
                sanitized, truncated = self._sanitize_value(item, key=None)
                result.append(sanitized)
                was_truncated = was_truncated or truncated
            return result, was_truncated
        if isinstance(value, dict):
            result_dict: dict[str, JsonValue] = {}
            was_truncated = False
            for child_key, child_value in value.items():
                sanitized, truncated = self._sanitize_value(
                    child_value,
                    key=child_key,
                )
                result_dict[child_key] = sanitized
                was_truncated = was_truncated or truncated
            return result_dict, was_truncated
        return value, False


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in _NON_SECRET_TOKEN_FIELDS:
        return False
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(("_api_key", "_password", "_secret", "_token"))
