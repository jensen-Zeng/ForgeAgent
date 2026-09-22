"""Tests for structured events, JSONL traces, and secret redaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from rich.console import Console

from forge_agent.tracing import (
    ConsoleEventSink,
    EventBus,
    EventKind,
    EventSink,
    JsonlTraceSink,
    MemoryEventSink,
    Redactor,
    RunTracer,
    TraceEvent,
    Visibility,
)

RUN_ID = UUID("12345678-1234-5678-1234-567812345678")
NOW = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    """Return a deterministic UTC timestamp."""
    return NOW


def make_tracer(
    *sinks: EventSink,
    redactor: Redactor | None = None,
) -> tuple[RunTracer, MemoryEventSink]:
    """Create a tracer with a memory sink plus optional additional sinks."""
    memory = MemoryEventSink()
    typed_sinks = (memory, *sinks)
    tracer = RunTracer(
        bus=EventBus(typed_sinks),
        run_id=RUN_ID,
        redactor=redactor,
        clock=fixed_clock,
    )
    return tracer, memory


def test_events_have_strict_sequences_same_run_id_and_utc_time() -> None:
    tracer, memory = make_tracer()

    tracer.emit(EventKind.RUN_STARTED)
    tracer.emit(EventKind.MODEL_REQUEST_STARTED)
    tracer.emit(EventKind.MODEL_RESPONSE_RECEIVED)

    assert [event.sequence for event in memory.events] == [1, 2, 3]
    assert {event.run_id for event in memory.events} == {RUN_ID}
    assert {event.timestamp for event in memory.events} == {NOW}
    assert {event.schema_version for event in memory.events} == {"1.0"}


def test_naive_event_timestamp_is_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        TraceEvent(
            run_id=RUN_ID,
            sequence=1,
            timestamp=datetime(2026, 7, 26, 8, 0),
            kind=EventKind.RUN_STARTED,
        )


def test_jsonl_contains_one_valid_json_object_per_line(tmp_path: Path) -> None:
    trace_path = tmp_path / "traces" / "run.jsonl"
    tracer, _ = make_tracer(JsonlTraceSink(trace_path))

    tracer.emit(EventKind.RUN_STARTED, data={"task": "inspect"})
    tracer.emit(EventKind.BUDGET_UPDATED, data={"remaining_steps": 29})

    lines = trace_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == 2
    assert [record["sequence"] for record in records] == [1, 2]
    assert [record["kind"] for record in records] == [
        "run_started",
        "budget_updated",
    ]


def test_known_secret_and_sensitive_keys_are_redacted(tmp_path: Path) -> None:
    secret = "private-development-key"
    trace_path = tmp_path / "run.jsonl"
    tracer, memory = make_tracer(
        JsonlTraceSink(trace_path),
        redactor=Redactor(known_secrets=(secret,)),
    )

    tracer.emit(
        EventKind.MODEL_REQUEST_STARTED,
        summary=f"Calling service with {secret}",
        data={
            "api_key": secret,
            "nested": {"Authorization": f"Bearer {secret}"},
            "safe": f"prefix {secret} suffix",
        },
    )

    serialized = trace_path.read_text(encoding="utf-8")
    assert secret not in serialized
    assert memory.events[0].summary == "Calling service with <redacted>"
    assert memory.events[0].data["api_key"] == "<redacted>"
    assert "<redacted>" in serialized


@pytest.mark.parametrize(
    "token",
    (
        "sk-abcdefgh12345678",
        "ghp_abcdefgh12345678",
        "AKIAABCDEFGHIJKLMNOP",
        "Bearer abcdefghijklmnop",
    ),
)
def test_common_token_formats_are_redacted(token: str) -> None:
    tracer, memory = make_tracer()

    tracer.emit(EventKind.MODEL_REQUEST_STARTED, data={"message": token})

    assert token not in str(memory.events[0].data)
    assert "<redacted>" in str(memory.events[0].data)


def test_token_usage_fields_are_not_mistaken_for_secrets() -> None:
    tracer, memory = make_tracer()

    tracer.emit(
        EventKind.BUDGET_UPDATED,
        data={"input_tokens": 100, "output_tokens": 20, "max_tokens": 1_000},
    )

    assert memory.events[0].data == {
        "input_tokens": 100,
        "output_tokens": 20,
        "max_tokens": 1_000,
    }


def test_long_tool_output_is_truncated_and_marked() -> None:
    tracer, memory = make_tracer(redactor=Redactor(max_string_chars=10))

    event = tracer.emit(
        EventKind.TOOL_CALL_COMPLETED,
        data={"output": "x" * 100},
    )

    assert event.truncated is True
    assert str(memory.events[0].data["output"]).endswith("...[truncated]")
    assert len(str(memory.events[0].data["output"])) < 100


def test_console_only_displays_user_visible_summary() -> None:
    output = StringIO()
    console = Console(file=output, color_system=None, force_terminal=False)
    tracer, _ = make_tracer(ConsoleEventSink(console))

    tracer.emit(
        EventKind.MODEL_REQUEST_STARTED,
        summary="private debug message",
        data={"prompt": "private prompt"},
        visibility=Visibility.DEBUG,
    )
    tracer.emit(
        EventKind.TOOL_CALL_COMPLETED,
        summary="Read README.md",
        data={"output": "private file content"},
        visibility=Visibility.USER,
    )

    rendered = output.getvalue()
    assert "Read README.md" in rendered
    assert "private debug message" not in rendered
    assert "private file content" not in rendered


def test_context_records_failure_then_reraises() -> None:
    tracer, memory = make_tracer()

    with pytest.raises(RuntimeError, match="boom"), tracer.run():
        raise RuntimeError("boom")

    assert [event.kind for event in memory.events] == [
        EventKind.RUN_STARTED,
        EventKind.RUN_FAILED,
    ]
    assert memory.events[-1].data["error_type"] == "RuntimeError"
    assert tracer.terminal_kind is EventKind.RUN_FAILED


def test_context_records_keyboard_interrupt_then_reraises() -> None:
    tracer, memory = make_tracer()

    with pytest.raises(KeyboardInterrupt), tracer.run():
        raise KeyboardInterrupt

    assert memory.events[-1].kind is EventKind.RUN_CANCELLED
    assert memory.events[-1].data["reason"] == "keyboard_interrupt"


def test_context_completes_when_no_terminal_event_was_emitted() -> None:
    tracer, memory = make_tracer()

    with tracer.run():
        tracer.emit(EventKind.BUDGET_UPDATED)

    assert [event.kind for event in memory.events] == [
        EventKind.RUN_STARTED,
        EventKind.BUDGET_UPDATED,
        EventKind.RUN_COMPLETED,
    ]


def test_event_after_terminal_event_is_rejected() -> None:
    tracer, _ = make_tracer()
    tracer.emit(EventKind.RUN_COMPLETED)

    with pytest.raises(RuntimeError, match="after terminal"):
        tracer.emit(EventKind.BUDGET_UPDATED)


def test_unwritable_trace_path_is_non_fatal_and_recorded(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    tracer, memory = make_tracer(JsonlTraceSink(blocker / "run.jsonl"))

    event = tracer.emit(EventKind.RUN_STARTED)

    assert event.kind is EventKind.RUN_STARTED
    assert memory.events == [event]
    assert len(tracer.sink_failures) == 1
    assert tracer.sink_failures[0].sink_name == "JsonlTraceSink"


def test_sink_serialization_failure_is_non_fatal_and_recorded() -> None:
    class FailingSink:
        def write(self, event: TraceEvent) -> None:
            del event
            raise TypeError("serialization failed for secret-value")

    secret = "secret-value"
    tracer, memory = make_tracer(
        FailingSink(),
        redactor=Redactor(known_secrets=(secret,)),
    )

    event = tracer.emit(EventKind.RUN_STARTED)

    assert memory.events == [event]
    assert len(tracer.sink_failures) == 1
    assert secret not in tracer.sink_failures[0].message
    assert "<redacted>" in tracer.sink_failures[0].message
