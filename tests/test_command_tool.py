"""Tests for command risk policy, approval, process bounds, and recovery loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from pydantic import JsonValue

from forge_agent.domain import (
    ModelResponse,
    RunConfig,
    RunStatus,
    StopReason,
    TextPart,
    ToolCall,
    ToolCallPart,
)
from forge_agent.models import FakeModelProvider
from forge_agent.policy import CommandPolicy, CommandRequest, RiskLevel
from forge_agent.runtime import AgentRuntime
from forge_agent.sandbox import DockerSandbox, DockerSandboxConfig, SandboxResult
from forge_agent.tools import ApplyPatchTool, RunCommandTool, ToolRegistry, Workspace
from forge_agent.tracing import EventBus, EventKind, MemoryEventSink, RunTracer

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")


def call(name: str, **arguments: object) -> ToolCall:
    """Build a JSON-compatible tool call."""
    return ToolCall(
        id="command-call",
        name=name,
        arguments=cast(dict[str, JsonValue], arguments),
    )


def run_call(
    executable: str,
    *args: str,
    cwd: str = ".",
    timeout_seconds: float = 10,
    max_output_chars: int = 50_000,
    **extra: object,
) -> ToolCall:
    """Build one structured command request."""
    return call(
        "run_command",
        executable=executable,
        args=list(args),
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        **extra,
    )


def python_policy() -> CommandPolicy:
    """Allow this test interpreter for deterministic subprocess fixtures."""
    return CommandPolicy(extra_safe_executables=(Path(sys.executable).name,))


def test_policy_classifies_safe_review_and_denied_commands() -> None:
    policy = CommandPolicy()

    safe = policy.classify(
        CommandRequest(executable="python", args=("-m", "pytest", "-q"), cwd=".")
    )
    review = policy.classify(
        CommandRequest(executable="python", args=("-m", "pip", "install", "x"), cwd=".")
    )
    denied = policy.classify(CommandRequest(executable="git", args=("reset", "--hard"), cwd="."))
    shell = policy.classify(
        CommandRequest(executable="pytest", args=("-q", "&&", "whoami"), cwd=".")
    )

    assert safe.risk is RiskLevel.SAFE
    assert review.risk is RiskLevel.REVIEW
    assert denied.risk is RiskLevel.DENY
    assert shell.risk is RiskLevel.DENY


def test_command_captures_exit_code_stdout_and_stderr(tmp_path: Path) -> None:
    tool = RunCommandTool(Workspace(tmp_path), policy=python_policy())
    script = "import sys; print('out'); print('err', file=sys.stderr); raise SystemExit(7)"

    result = tool.execute(run_call(sys.executable, "-c", script))

    assert result.ok is True
    assert result.metadata["exit_code"] == 7
    assert "stdout:\nout" in result.output
    assert "stderr:\nerr" in result.output
    assert result.metadata["timed_out"] is False


def test_command_timeout_kills_process_before_late_side_effect(tmp_path: Path) -> None:
    marker = tmp_path / "late.txt"
    tool = RunCommandTool(Workspace(tmp_path), policy=python_policy())
    script = (
        "import pathlib,time;"
        "time.sleep(2);"
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )

    result = tool.execute(run_call(sys.executable, "-c", script, timeout_seconds=0.05))

    assert result.ok is True
    assert result.metadata["timed_out"] is True
    assert marker.exists() is False


def test_timeout_falls_back_to_direct_kill_when_tree_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "late-fallback.txt"
    tool = RunCommandTool(Workspace(tmp_path), policy=python_policy())
    script = (
        "import pathlib,time;"
        "time.sleep(2);"
        f"pathlib.Path({str(marker)!r}).write_text('late', encoding='utf-8')"
    )

    def taskkill_failure(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated taskkill failure")

    monkeypatch.setattr("forge_agent.tools.command.subprocess.run", taskkill_failure)
    result = tool.execute(run_call(sys.executable, "-c", script, timeout_seconds=0.05))

    assert result.ok is True
    assert result.metadata["timed_out"] is True
    assert marker.exists() is False


def test_large_stdout_and_stderr_are_truncated_separately(tmp_path: Path) -> None:
    tool = RunCommandTool(Workspace(tmp_path), policy=python_policy())
    script = "import sys; print('o'*200); print('e'*200, file=sys.stderr)"

    result = tool.execute(run_call(sys.executable, "-c", script, max_output_chars=50))

    assert result.metadata["stdout_truncated"] is True
    assert result.metadata["stderr_truncated"] is True
    assert result.output.count("...[truncated]") == 2


def test_command_supports_space_and_chinese_working_directory(tmp_path: Path) -> None:
    child = tmp_path / "有 空格"
    child.mkdir()
    tool = RunCommandTool(Workspace(tmp_path), policy=python_policy())

    result = tool.execute(
        run_call(sys.executable, "-c", "import os; print(os.getcwd())", cwd="有 空格")
    )

    assert result.ok is True
    assert "有 空格" in result.output


def test_subprocess_environment_does_not_receive_api_key(tmp_path: Path) -> None:
    tool = RunCommandTool(
        Workspace(tmp_path),
        policy=python_policy(),
        environ={
            "PATH": str(Path(sys.executable).parent),
            "FORGE_MODEL_API_KEY": "private-key",
        },
    )
    script = "import os; print(os.getenv('FORGE_MODEL_API_KEY'))"

    result = tool.execute(run_call(sys.executable, "-c", script))

    assert result.ok is True
    assert "None" in result.output
    assert "private-key" not in result.output


@pytest.mark.parametrize(
    ("executable", "args"),
    (
        ("pwsh", ("-Command", "Get-ChildItem")),
        ("curl", ("https://example.com",)),
        ("git", ("push",)),
        ("rm", ("-rf", ".")),
        ("sudo", ("pytest",)),
        ("unknown-program", ()),
    ),
)
def test_dangerous_or_unknown_commands_are_denied(
    tmp_path: Path,
    executable: str,
    args: tuple[str, ...],
) -> None:
    tool = RunCommandTool(Workspace(tmp_path))

    result = tool.execute(run_call(executable, *args))

    assert result.ok is False
    assert result.metadata["error_code"] == "command_denied"
    assert result.metadata["risk"] == "deny"
    assert result.metadata["backend"] == "none"
    assert result.metadata["sandboxed"] is False


def test_review_command_requires_local_approval_and_records_decision(
    tmp_path: Path,
) -> None:
    decisions: list[str] = []

    def approve(review: object) -> bool:
        decisions.append(type(review).__name__)
        return True

    tool = RunCommandTool(
        Workspace(tmp_path),
        approval_handler=approve,
    )
    result = tool.execute(run_call("python", "-m", "pip", "--version"))

    assert decisions == ["CommandReview"]
    assert result.ok is True
    assert result.metadata["risk"] == "review"


def test_review_rejection_is_structured_and_model_cannot_forge_approval(
    tmp_path: Path,
) -> None:
    tool = RunCommandTool(
        Workspace(tmp_path),
        approval_handler=lambda review: False,
    )

    rejected = tool.execute(run_call("python", "-m", "pip", "--version"))
    forged = tool.execute(run_call("python", "-m", "pip", "--version", approved=True))

    assert rejected.metadata["error_code"] == "approval_denied"
    assert forged.metadata["error_code"] == "invalid_arguments"


def test_approval_events_are_written_to_trace(tmp_path: Path) -> None:
    memory = MemoryEventSink()
    tracer = RunTracer(bus=EventBus((memory,)), run_id=RUN_ID)
    tool = RunCommandTool(
        Workspace(tmp_path),
        approval_handler=lambda review: False,
        tracer=tracer,
    )

    tool.execute(run_call("python", "-m", "pip", "--version"))

    assert [event.kind for event in memory.events] == [
        EventKind.APPROVAL_REQUESTED,
        EventKind.APPROVAL_RESOLVED,
    ]
    assert memory.events[-1].data["approved"] is False


@pytest.mark.asyncio
async def test_runtime_stops_with_user_denied_reason(tmp_path: Path) -> None:
    command = run_call("python", "-m", "pip", "--version")
    provider = FakeModelProvider((ModelResponse(content=(ToolCallPart(tool_call=command),)),))
    tool = RunCommandTool(
        Workspace(tmp_path),
        approval_handler=lambda review: False,
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((tool,)),
    ).run(RunConfig(task="Install something", workspace=tmp_path))

    assert result.status is RunStatus.STOPPED
    assert result.stop_reason is StopReason.USER_DENIED


@pytest.mark.asyncio
async def test_bug_fix_loop_fails_patches_then_passes_tests(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text(
        "def add(left: int, right: int) -> int:\n    return left - right\n",
        encoding="utf-8",
    )
    (tmp_path / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    first_test = run_call(sys.executable, "-m", "pytest", "-q")
    patch = call(
        "apply_patch",
        edits=[
            {
                "path": "calc.py",
                "old_text": "return left - right",
                "new_text": "return left + right",
            }
        ],
    )
    second_test = first_test.model_copy(update={"id": "test-again"})
    provider = FakeModelProvider(
        (
            ModelResponse(content=(ToolCallPart(tool_call=first_test),)),
            ModelResponse(content=(ToolCallPart(tool_call=patch),)),
            ModelResponse(content=(ToolCallPart(tool_call=second_test),)),
            ModelResponse(content=(TextPart(text="Bug fixed and tests pass."),)),
        )
    )
    registry = ToolRegistry(
        (
            RunCommandTool(Workspace(tmp_path)),
            ApplyPatchTool(Workspace(tmp_path)),
        )
    )

    result = await AgentRuntime(provider=provider, tools=registry).run(
        RunConfig(task="Fix add", workspace=tmp_path)
    )

    first_observation = result.messages[3].content[0].tool_result  # type: ignore[union-attr]
    final_test_observation = result.messages[7].content[0].tool_result  # type: ignore[union-attr]
    assert first_observation.metadata["exit_code"] == 1
    assert final_test_observation.metadata["exit_code"] == 0, final_test_observation.output
    assert result.status is RunStatus.COMPLETED
    assert result.changed_files == ("calc.py",)
    assert "return left + right" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_command_output_metadata_is_json_serializable(tmp_path: Path) -> None:
    result = RunCommandTool(Workspace(tmp_path), policy=python_policy()).execute(
        run_call(sys.executable, "-c", "print('ok')")
    )

    json.dumps(result.model_dump(mode="json"))


def test_safe_command_can_use_docker_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )
    observed: dict[str, object] = {}

    def fake_run(
        command: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
        working_directory: str = ".",
    ) -> SandboxResult:
        observed["command"] = command
        observed["timeout_seconds"] = timeout_seconds
        observed["working_directory"] = working_directory
        return SandboxResult(
            exit_code=0,
            stdout="container test passed",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(sandbox, "run", fake_run)
    tool = RunCommandTool(Workspace(tmp_path), sandbox=sandbox)

    result = tool.execute(run_call("python", "-m", "pytest", "-q", timeout_seconds=7))

    assert result.ok is True
    assert result.metadata["backend"] == "docker"
    assert observed["command"] == ("python", "-m", "pytest", "-q")
    assert observed["timeout_seconds"] == 7
    assert observed["working_directory"] == "."
    assert "container test passed" in result.output


def test_auto_mode_routes_review_command_to_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )
    observed: dict[str, object] = {}

    def fake_run(
        command: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
        working_directory: str = ".",
    ) -> SandboxResult:
        observed["command"] = command
        observed["working_directory"] = working_directory
        return SandboxResult(exit_code=0, stdout="sandboxed", stderr="", timed_out=False)

    monkeypatch.setattr(sandbox, "run", fake_run)
    tool = RunCommandTool(
        Workspace(tmp_path),
        sandbox=sandbox,
        execution_mode="auto",
        approval_handler=lambda review: review.execution_backend == "docker",
    )

    result = tool.execute(run_call("python", "-m", "pip", "--version"))

    assert result.ok is True
    assert result.metadata["risk"] == "review"
    assert result.metadata["backend"] == "docker"
    assert result.metadata["sandboxed"] is True
    assert result.metadata["sandbox_required"] is True
    routing_reason = result.metadata["routing_reason"]
    assert isinstance(routing_reason, str)
    assert "automatically routed" in routing_reason
    assert observed["command"] == ("python", "-m", "pip", "--version")


def test_auto_mode_keeps_safe_command_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )

    def unexpected_run(*args: object, **kwargs: object) -> SandboxResult:
        del args, kwargs
        raise AssertionError("safe command should not enter Docker in auto mode")

    monkeypatch.setattr(sandbox, "run", unexpected_run)
    tool = RunCommandTool(
        Workspace(tmp_path),
        policy=python_policy(),
        sandbox=sandbox,
        execution_mode="auto",
    )

    result = tool.execute(run_call(sys.executable, "-c", "print('local-safe')"))

    assert result.ok is True
    assert result.metadata["risk"] == "safe"
    assert result.metadata["backend"] == "local"
    assert result.metadata["sandboxed"] is False
    assert result.metadata["sandbox_required"] is False
    assert "local-safe" in result.output


def test_auto_mode_fails_closed_when_docker_is_unavailable(tmp_path: Path) -> None:
    sandbox = DockerSandbox(DockerSandboxConfig(workspace=tmp_path))
    sandbox._docker = None
    tool = RunCommandTool(
        Workspace(tmp_path),
        sandbox=sandbox,
        execution_mode="auto",
        approval_handler=lambda review: True,
    )

    result = tool.execute(run_call("python", "-m", "pip", "--version"))

    assert result.ok is False
    assert result.metadata["error_code"] == "sandbox_unavailable"
    assert result.metadata["risk"] == "review"
    assert result.metadata["backend"] == "docker"
    assert result.metadata["sandboxed"] is False
    assert result.metadata["sandbox_required"] is True
    assert result.metadata["fallback"] == "denied"


def test_auto_or_docker_mode_requires_sandbox_object(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="auto execution requires"):
        RunCommandTool(Workspace(tmp_path), execution_mode="auto")
    with pytest.raises(ValueError, match="docker execution requires"):
        RunCommandTool(Workspace(tmp_path), execution_mode="docker")
