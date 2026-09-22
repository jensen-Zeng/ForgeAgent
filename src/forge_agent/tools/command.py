"""Policy-gated subprocess execution with bounded output and cleanup."""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from forge_agent.policy import CommandPolicy, CommandRequest, CommandReview, RiskLevel
from forge_agent.processes import WINDOWS_CREATE_NEW_PROCESS_GROUP, terminate_process_tree
from forge_agent.sandbox import DockerSandbox, SandboxUnavailableError
from forge_agent.tools.base import BaseTool, ToolError, ToolExecution
from forge_agent.tools.workspace import Workspace, WorkspaceError
from forge_agent.tracing import EventKind, RunTracer

ApprovalHandler = Callable[[CommandReview], bool]
_SENSITIVE_ENV_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
)


class RunCommandParameters(BaseModel):
    """Strict command arguments with no shell expression field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executable: str = Field(min_length=1, max_length=500)
    args: tuple[str, ...] = Field(default=(), max_length=100)
    cwd: str = "."
    timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    max_output_chars: int = Field(default=50_000, gt=0, le=200_000)


class RunCommandTool(BaseTool[RunCommandParameters]):
    """Execute a narrowly classified command without invoking a shell."""

    name = "run_command"
    description = (
        "Run an allowlisted test or static-check command using executable and args. "
        "Shell syntax, network tools, privilege escalation and destructive commands are denied."
    )
    parameters_type: ClassVar[type[BaseModel]] = RunCommandParameters

    def __init__(
        self,
        workspace: Workspace,
        *,
        policy: CommandPolicy | None = None,
        approval_handler: ApprovalHandler | None = None,
        approval_mode: str = "risky",
        tracer: RunTracer | None = None,
        environ: Mapping[str, str] | None = None,
        sandbox: DockerSandbox | None = None,
        execution_mode: Literal["local", "docker", "auto"] | None = None,
    ) -> None:
        resolved_mode = execution_mode or ("docker" if sandbox is not None else "local")
        if resolved_mode in {"docker", "auto"} and sandbox is None:
            raise ValueError(f"{resolved_mode} execution requires a Docker sandbox")
        self._workspace = workspace
        self._policy = policy or CommandPolicy()
        self._approval_handler = approval_handler
        self._approval_mode = approval_mode
        self._tracer = tracer
        self._environ = dict(environ or os.environ)
        self._sandbox = sandbox
        self._execution_mode = resolved_mode

    def run(self, parameters: RunCommandParameters) -> ToolExecution:
        try:
            cwd = self._workspace.resolve(parameters.cwd, require_directory=True)
        except WorkspaceError as error:
            raise ToolError(str(error), code=error.code) from error
        request = CommandRequest(
            executable=parameters.executable,
            args=parameters.args,
            cwd=self._workspace.display_path(cwd),
        )
        review = self._route(self._policy.classify(request))
        approved = self._authorize(review)
        if not approved:
            code = "command_denied" if review.risk is RiskLevel.DENY else "approval_denied"
            raise ToolError(
                f"Command rejected: {review.reason}",
                code=code,
                metadata={
                    "risk": review.risk.value,
                    "reason": review.reason,
                    "backend": review.execution_backend,
                    "sandboxed": False,
                    "sandbox_required": review.execution_backend == "docker",
                    "routing_reason": review.routing_reason,
                },
            )
        return self._execute(parameters, cwd, review)

    def _route(self, review: CommandReview) -> CommandReview:
        """Choose an execution boundary from local policy, never model input."""
        if review.risk is RiskLevel.DENY:
            backend = "none"
            reason = "denied by policy before execution backend selection"
        elif self._execution_mode == "docker":
            backend = "docker"
            reason = "all allowed commands are forced into the configured sandbox"
        elif self._execution_mode == "auto" and review.risk is RiskLevel.REVIEW:
            backend = "docker"
            reason = "review-risk command automatically routed to the sandbox"
        elif self._execution_mode == "auto":
            backend = "local"
            reason = "safe command kept local by automatic risk routing"
        else:
            backend = "local"
            reason = "local execution explicitly configured"
        return review.model_copy(
            update={
                "execution_backend": backend,
                "routing_reason": reason,
            }
        )

    def _authorize(self, review: CommandReview) -> bool:
        if review.risk is RiskLevel.DENY:
            return False
        needs_approval = review.risk is RiskLevel.REVIEW or self._approval_mode == "always"
        if not needs_approval:
            return True
        self._emit_approval(EventKind.APPROVAL_REQUESTED, review, approved=None)
        approved = (
            self._approval_mode != "never"
            and self._approval_handler is not None
            and self._approval_handler(review)
        )
        self._emit_approval(EventKind.APPROVAL_RESOLVED, review, approved=approved)
        return approved

    def _execute(
        self,
        parameters: RunCommandParameters,
        cwd: Path,
        review: CommandReview,
    ) -> ToolExecution:
        command = [parameters.executable, *parameters.args]
        if review.execution_backend == "docker":
            return self._execute_in_sandbox(
                tuple(command),
                review,
                timeout_seconds=parameters.timeout_seconds,
                max_output_chars=parameters.max_output_chars,
                working_directory=self._workspace.display_path(cwd),
            )
        started = time.monotonic()
        timed_out = False
        return_code: int
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env=self._sanitized_environment(),
                    shell=False,
                    start_new_session=os.name != "nt",
                    creationflags=WINDOWS_CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as error:
                raise ToolError(
                    f"Unable to start command: {type(error).__name__}",
                    code="command_start_error",
                ) from error
            try:
                return_code = process.wait(timeout=parameters.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process_group(process)
                return_code = process.returncode if process.returncode is not None else -1
            except KeyboardInterrupt:
                self._terminate_process_group(process)
                raise
            stdout = self._read_bounded(
                cast(BinaryIO, stdout_file),
                parameters.max_output_chars,
            )
            stderr = self._read_bounded(
                cast(BinaryIO, stderr_file),
                parameters.max_output_chars,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_text, stdout_truncated = stdout
        stderr_text, stderr_truncated = stderr
        output = (
            f"exit_code: {return_code}\n"
            f"timed_out: {str(timed_out).lower()}\n"
            f"stdout:\n{stdout_text or '(empty)'}\n"
            f"stderr:\n{stderr_text or '(empty)'}"
        )
        return ToolExecution(
            output=output,
            metadata={
                "exit_code": return_code,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "risk": review.risk.value,
                "backend": "local",
                "sandboxed": False,
                "sandbox_required": False,
                "routing_reason": review.routing_reason,
            },
        )

    def _execute_in_sandbox(
        self,
        command: tuple[str, ...],
        review: CommandReview,
        *,
        timeout_seconds: float,
        max_output_chars: int,
        working_directory: str,
    ) -> ToolExecution:
        started = time.monotonic()
        try:
            result = (
                self._sandbox.run(
                    command,
                    timeout_seconds=timeout_seconds,
                    working_directory=working_directory,
                )
                if self._sandbox is not None
                else None
            )
        except SandboxUnavailableError as error:
            raise ToolError(
                str(error),
                code="sandbox_unavailable",
                metadata={
                    "risk": review.risk.value,
                    "backend": "docker",
                    "sandboxed": False,
                    "sandbox_required": True,
                    "routing_reason": review.routing_reason,
                    "fallback": "denied",
                },
            ) from error
        if result is None:
            raise AssertionError("sandbox execution selected without a sandbox")
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = result.stdout[-max_output_chars:]
        stderr = result.stderr[-max_output_chars:]
        stdout_truncated = len(result.stdout) > max_output_chars
        stderr_truncated = len(result.stderr) > max_output_chars
        if stdout_truncated:
            stdout = "...[truncated]\n" + stdout
        if stderr_truncated:
            stderr = "...[truncated]\n" + stderr
        output = (
            f"exit_code: {result.exit_code}\n"
            f"timed_out: {str(result.timed_out).lower()}\n"
            f"stdout:\n{stdout or '(empty)'}\n"
            f"stderr:\n{stderr or '(empty)'}"
        )
        return ToolExecution(
            output=output,
            metadata={
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": duration_ms,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "risk": review.risk.value,
                "backend": "docker",
                "sandboxed": True,
                "sandbox_required": True,
                "routing_reason": review.routing_reason,
            },
        )

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        terminate_process_tree(process)

    def _read_bounded(
        self,
        stream: BinaryIO,
        limit: int,
    ) -> tuple[str, bool]:
        stream.seek(0)
        raw = stream.read(limit + 1)
        truncated = len(raw) > limit
        if truncated:
            raw = raw[:limit]
        text = raw.decode("utf-8", errors="replace")
        if truncated:
            text += "\n...[truncated]"
        return text.rstrip(), truncated

    def _sanitized_environment(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in self._environ.items():
            upper = key.upper()
            if upper not in _ENV_ALLOWLIST:
                continue
            if any(marker in upper for marker in _SENSITIVE_ENV_MARKERS):
                continue
            result[key] = value
        result["PYTHONUTF8"] = "1"
        return result

    def _emit_approval(
        self,
        kind: EventKind,
        review: CommandReview,
        *,
        approved: bool | None,
    ) -> None:
        if self._tracer is not None:
            self._tracer.emit(
                kind,
                data={
                    "executable": review.request.executable,
                    "args": cast(list[JsonValue], list(review.request.args)),
                    "cwd": review.request.cwd,
                    "risk": review.risk.value,
                    "reason": review.reason,
                    "backend": review.execution_backend,
                    "sandboxed": False,
                    "sandbox_required": review.execution_backend == "docker",
                    "routing_reason": review.routing_reason,
                    "approved": approved,
                },
            )
