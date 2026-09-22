"""Event subscribers for memory, console, and durable JSONL traces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rich.console import Console
from rich.text import Text

from forge_agent.tracing.events import EventKind, TraceEvent, Visibility


class EventSink(Protocol):
    """Consumer of already sanitized trace events."""

    def write(self, event: TraceEvent) -> None:
        """Consume one event."""


@dataclass(frozen=True, slots=True)
class SinkFailure:
    """Non-fatal record of a sink that could not consume an event."""

    sink_name: str
    event_kind: EventKind
    error_type: str
    message: str


class EventBus:
    """Synchronously fan out events while isolating subscriber failures."""

    def __init__(self, sinks: tuple[EventSink, ...] = ()) -> None:
        self._sinks: list[EventSink] = list(sinks)

    def subscribe(self, sink: EventSink) -> None:
        """Register a new event subscriber."""
        self._sinks.append(sink)

    def publish(self, event: TraceEvent) -> tuple[SinkFailure, ...]:
        """Deliver an event to every sink without failing the main run."""
        failures: list[SinkFailure] = []
        for sink in tuple(self._sinks):
            try:
                sink.write(event)
            except Exception as error:
                failures.append(
                    SinkFailure(
                        sink_name=type(sink).__name__,
                        event_kind=event.kind,
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
        return tuple(failures)


class MemoryEventSink:
    """In-memory sink for deterministic tests and event assertions."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def write(self, event: TraceEvent) -> None:
        """Append one event in publication order."""
        self.events.append(event)


class ConsoleEventSink:
    """Display only user-visible summaries, never raw debug payloads."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or Console()

    def write(self, event: TraceEvent) -> None:
        """Render a concise event summary when it is safe for users."""
        if event.visibility is Visibility.USER and event.summary:
            line = Text()
            line.append(event.kind.value, style="bold cyan")
            line.append(": ")
            line.append(event.summary)
            self._console.print(line)


class TraceWriteError(OSError):
    """Raised internally when a JSONL trace cannot be persisted."""


class JsonlTraceSink:
    """Append each event as one independently parseable UTF-8 JSON line."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve(strict=False)

    def write(self, event: TraceEvent) -> None:
        """Append and flush one event, wrapping filesystem/serialization errors."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            serialized = event.model_dump_json()
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(serialized)
                stream.write("\n")
                stream.flush()
        except (OSError, TypeError, ValueError) as error:
            raise TraceWriteError(f"Unable to write trace {self.path}: {error}") from error
