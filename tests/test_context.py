"""Tests for repository context, compression, planning and progress detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.context import ContextManager
from forge_agent.domain import (
    Message,
    ModelResponse,
    Role,
    RunConfig,
    RunStatus,
    StopReason,
    TextPart,
    ToolCall,
    ToolCallPart,
    ToolResult,
    ToolResultPart,
)
from forge_agent.models import FakeModelProvider
from forge_agent.runtime import AgentRuntime
from forge_agent.tools import ListFilesTool, ToolRegistry, Workspace


def call(call_id: str, name: str, **arguments: object) -> ToolCall:
    """Build a compact tool call with JSON-compatible fixtures."""
    return ToolCall(id=call_id, name=name, arguments=arguments)  # type: ignore[arg-type]


def result(
    call_id: str,
    name: str,
    *,
    output: str = "ok",
    ok: bool = True,
    error: str | None = None,
    metadata: dict[str, object] | None = None,
) -> ToolResult:
    """Build a compact result fixture."""
    return ToolResult(
        call_id=call_id,
        name=name,
        ok=ok,
        output=output,
        error=error,
        metadata=metadata or {},  # type: ignore[arg-type]
    )


def test_initial_context_loads_rules_by_priority_and_safe_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text("Highest project rule", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("Secondary rule", encoding="utf-8")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hidden\n", encoding="utf-8")
    manager = ContextManager(Workspace(tmp_path))

    messages = manager.start(RunConfig(task="Inspect", workspace=tmp_path))

    system_text = messages[0].content[0].text  # type: ignore[union-attr]
    assert system_text.index("Highest project rule") < system_text.index("Secondary rule")
    assert "app.py" in system_text
    assert ".env" not in system_text
    assert "untrusted project data" in system_text
    assert messages[1].role is Role.USER


def test_duplicate_tool_results_are_replaced_by_reference(tmp_path: Path) -> None:
    manager = ContextManager(Workspace(tmp_path))
    manager.start(RunConfig(task="Read", workspace=tmp_path))
    first_call = call("read-1", "read_file", path="app.py")
    second_call = call("read-2", "read_file", path="app.py")

    first = manager.process_result(first_call, result("read-1", "read_file", output="same"))
    second = manager.process_result(
        second_call,
        result("read-2", "read_file", output="same"),
    )

    assert first.output == "same"
    assert second.output.startswith("[duplicate observation")
    assert second.metadata["duplicate_of_call_id"] == "read-1"
    assert manager.snapshot().deduplicated_results == 1


def test_patch_invalidates_old_read_generation(tmp_path: Path) -> None:
    manager = ContextManager(Workspace(tmp_path))
    manager.start(RunConfig(task="Change", workspace=tmp_path))
    manager.process_result(
        call("read-1", "read_file", path="app.py"),
        result("read-1", "read_file", output="VALUE = 1"),
    )
    duplicate = manager.process_result(
        call("read-2", "read_file", path="app.py"),
        result("read-2", "read_file", output="VALUE = 1"),
    )
    manager.process_result(
        call("patch", "apply_patch"),
        result(
            "patch",
            "apply_patch",
            output="diff",
            metadata={"changed_files": ["app.py"]},
        ),
    )
    fresh = manager.process_result(
        call("read-3", "read_file", path="app.py"),
        result("read-3", "read_file", output="VALUE = 1"),
    )

    assert duplicate.metadata["context_deduplicated"] is True
    assert "context_deduplicated" not in fresh.metadata


def test_old_successful_observations_are_compressed_near_budget(
    tmp_path: Path,
) -> None:
    manager = ContextManager(
        Workspace(tmp_path),
        max_tokens=300,
        compression_ratio=0.5,
        preserve_recent_messages=2,
    )
    initial = list(manager.start(RunConfig(task="Inspect", workspace=tmp_path)))
    messages = [
        *initial,
        Message(
            role=Role.ASSISTANT,
            content=(ToolCallPart(tool_call=call("one", "read_file", path="a.py")),),
        ),
        Message(
            role=Role.TOOL,
            content=(ToolResultPart(tool_result=result("one", "read_file", output="x" * 2000)),),
        ),
        Message(role=Role.ASSISTANT, content=(TextPart(text="continue"),)),
        Message(role=Role.USER, content=(TextPart(text="latest"),)),
    ]

    optimized = manager.optimize(messages, ())

    compressed = optimized[3].content[0].tool_result  # type: ignore[union-attr]
    assert compressed.output.startswith("[compressed observation")
    assert compressed.metadata["context_compressed"] is True
    assert manager.snapshot().compressions == 1
    assert manager.snapshot().estimated_tokens < manager.estimate_tokens(messages, ())


def test_failed_observations_are_preserved_during_compression(tmp_path: Path) -> None:
    manager = ContextManager(
        Workspace(tmp_path),
        max_tokens=100,
        compression_ratio=0.5,
        preserve_recent_messages=2,
    )
    messages = list(manager.start(RunConfig(task="Inspect", workspace=tmp_path)))
    failure = result(
        "bad",
        "read_file",
        ok=False,
        error="critical error",
        output="",
    )
    messages.extend(
        (
            Message(
                role=Role.ASSISTANT,
                content=(ToolCallPart(tool_call=call("bad", "read_file", path="x")),),
            ),
            Message(
                role=Role.TOOL,
                content=(ToolResultPart(tool_result=failure),),
            ),
            Message(role=Role.USER, content=(TextPart(text="latest"),)),
            Message(role=Role.ASSISTANT, content=(TextPart(text="working"),)),
        )
    )

    optimized = manager.optimize(messages, ())

    preserved = optimized[3].content[0].tool_result  # type: ignore[union-attr]
    assert preserved.error == "critical error"


def test_repeated_identical_call_stops_before_third_execution(tmp_path: Path) -> None:
    manager = ContextManager(Workspace(tmp_path), repeated_call_limit=3)
    manager.start(RunConfig(task="Inspect", workspace=tmp_path))
    repeated = call("same", "list_files", path=".")

    assert manager.before_tool(repeated) is None
    assert manager.before_tool(repeated) is None
    assert manager.before_tool(repeated) is StopReason.NO_PROGRESS
    assert manager.snapshot().repeated_tool_calls == 2


def test_read_call_is_allowed_again_after_patch_invalidation(tmp_path: Path) -> None:
    manager = ContextManager(Workspace(tmp_path), repeated_call_limit=2)
    manager.start(RunConfig(task="Change", workspace=tmp_path))
    read = call("read", "read_file", path="app.py")

    assert manager.before_tool(read) is None
    manager.process_result(
        call("patch", "apply_patch"),
        result(
            "patch",
            "apply_patch",
            metadata={"changed_files": ["app.py"]},
        ),
    )
    assert manager.before_tool(read) is None


def test_test_result_abab_pattern_is_detected_as_oscillation(tmp_path: Path) -> None:
    manager = ContextManager(Workspace(tmp_path))
    manager.start(RunConfig(task="Fix", workspace=tmp_path))
    command = call("test", "run_command", executable="pytest")

    reasons = [
        manager.after_tool(
            command,
            result(
                f"test-{index}",
                "run_command",
                output=output,
                metadata={"exit_code": 1},
            ),
        )
        for index, output in enumerate(("error A", "error B", "error A", "error B"))
    ]

    assert reasons[-1] is StopReason.TEST_OSCILLATION


def test_recoverability_and_public_plan_are_updated(tmp_path: Path) -> None:
    manager = ContextManager(Workspace(tmp_path))
    manager.start(RunConfig(task="Fix", workspace=tmp_path))

    unsafe = manager.process_result(
        call("bad", "read_file", path="../secret"),
        result(
            "bad",
            "read_file",
            ok=False,
            error="outside",
            metadata={"error_code": "path_outside_workspace"},
        ),
    )
    manager.process_result(
        call("read", "read_file", path="app.py"),
        result("read", "read_file"),
    )
    manager.process_result(
        call("patch", "apply_patch"),
        result(
            "patch",
            "apply_patch",
            metadata={"changed_files": ["app.py"]},
        ),
    )
    manager.process_result(
        call("test", "run_command"),
        result("test", "run_command", metadata={"exit_code": 0}),
    )

    assert unsafe.metadata["recoverability"] == "unrecoverable"
    assert [step.status.value for step in manager.plan.steps] == [
        "completed",
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_runtime_no_progress_returns_handoff_and_context_metrics(
    tmp_path: Path,
) -> None:
    repeated = call("list", "list_files", path=".")
    provider = FakeModelProvider(
        tuple(ModelResponse(content=(ToolCallPart(tool_call=repeated),)) for _ in range(3))
    )
    workspace = Workspace(tmp_path)

    run_result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((ListFilesTool(workspace),)),
        context_manager=ContextManager(workspace, repeated_call_limit=3),
    ).run(RunConfig(task="Inspect files", workspace=tmp_path))

    assert run_result.status is RunStatus.STOPPED
    assert run_result.stop_reason is StopReason.NO_PROGRESS
    assert run_result.metrics.tool_calls == 2
    assert run_result.metrics.repeated_tool_calls == 2
    assert run_result.handoff_report is not None
    assert "Stop reason: no_progress" in run_result.handoff_report
    assert run_result.plan is not None
