"""Run-scoped event sequencing, sanitization, and terminal-state handling."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from pydantic import JsonValue

from forge_agent.tracing.events import EventKind, TraceEvent, Visibility
from forge_agent.tracing.redaction import Redactor
from forge_agent.tracing.sinks import EventBus, SinkFailure

Clock = Callable[[], datetime]
_TERMINAL_EVENTS = frozenset(
    {
        EventKind.RUN_COMPLETED,
        EventKind.RUN_STOPPED,
        EventKind.RUN_FAILED,
        EventKind.RUN_CANCELLED,
    }
)


def utc_now() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(UTC)


class RunTracer:
    """Create ordered, sanitized events for exactly one agent run."""

    def __init__(
        self,
        *,
        bus: EventBus,
        run_id: UUID | None = None,
        session_id: UUID | None = None,
        generation: int = 0,
        redactor: Redactor | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self.run_id = run_id or uuid4()
        self.session_id = session_id
        self.generation = generation
        self._bus = bus
        self._redactor = redactor or Redactor()
        self._clock = clock
        self._sequence = 0
        self._terminal_kind: EventKind | None = None
        self._sink_failures: list[SinkFailure] = []
        self._lock = Lock()

    @property
    def sequence(self) -> int:
        """Return the latest allocated sequence number."""
        return self._sequence

    @property
    def terminal_kind(self) -> EventKind | None:
        """Return the terminal event emitted for this run, if any."""
        return self._terminal_kind

    @property
    def sink_failures(self) -> tuple[SinkFailure, ...]:
        """Return trace sink failures without exposing a mutable collection."""
        return tuple(self._sink_failures)

    def emit(
        self,
        kind: EventKind,
        *,
        summary: str | None = None,
        data: Mapping[str, JsonValue] | None = None,
        visibility: Visibility = Visibility.DEBUG,
        correlation_id: str | None = None,
    ) -> TraceEvent:
        """Sanitize, sequence, and publish one event atomically."""
        with self._lock:
            if self._terminal_kind is not None:
                raise RuntimeError(
                    f"cannot emit {kind.value} after terminal event {self._terminal_kind.value}"
                )
            sanitized_summary: str | None = None
            summary_truncated = False
            if summary is not None:
                sanitized_summary, summary_truncated = self._redactor.sanitize_text(summary)
            sanitized_data, data_truncated = self._redactor.sanitize_mapping(data or {})
            self._sequence += 1
            event = TraceEvent(
                run_id=self.run_id,
                session_id=self.session_id,
                generation=self.generation,
                correlation_id=correlation_id,
                sequence=self._sequence,
                timestamp=self._clock(),
                kind=kind,
                visibility=visibility,
                summary=sanitized_summary,
                data=sanitized_data,
                truncated=summary_truncated or data_truncated,
            )
            failures = self._bus.publish(event)
            self._sink_failures.extend(self._sanitize_failures(failures))
            if kind in _TERMINAL_EVENTS:
                self._terminal_kind = kind
            return event

    @contextmanager
    def run(
        self,
        *,
        started_summary: str = "Run started.",
        completed_summary: str = "Run completed.",
    ) -> Iterator[RunTracer]:
        """Emit start/final events and preserve exceptions for the caller."""
        self.emit(
            EventKind.RUN_STARTED,
            summary=started_summary,
            visibility=Visibility.USER,
        )
        try:
            yield self
        except KeyboardInterrupt:
            if self._terminal_kind is None:
                self.emit(
                    EventKind.RUN_CANCELLED,
                    summary="Run cancelled by user.",
                    data={"reason": "keyboard_interrupt"},
                    visibility=Visibility.USER,
                )
            raise
        except BaseException as error:
            if self._terminal_kind is None:
                self.emit(
                    EventKind.RUN_FAILED,
                    summary="Run failed.",
                    data={
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                    visibility=Visibility.USER,
                )
            raise
        else:
            if self._terminal_kind is None:
                self.emit(
                    EventKind.RUN_COMPLETED,
                    summary=completed_summary,
                    visibility=Visibility.USER,
                )

    def _sanitize_failures(
        self,
        failures: tuple[SinkFailure, ...],
    ) -> tuple[SinkFailure, ...]:
        sanitized: list[SinkFailure] = []
        for failure in failures:
            message, _ = self._redactor.sanitize_text(failure.message)
            sanitized.append(
                SinkFailure(
                    sink_name=failure.sink_name,
                    event_kind=failure.event_kind,
                    error_type=failure.error_type,
                    message=message,
                )
            )
        return tuple(sanitized)
