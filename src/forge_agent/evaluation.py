"""Resettable evaluation harness, scoring, metrics, and reports."""

from __future__ import annotations

import asyncio
import difflib
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from forge_agent.config import ForgeSettings
from forge_agent.context import ContextManager
from forge_agent.domain import RunResult
from forge_agent.models import create_model_provider
from forge_agent.runtime import AgentRuntime
from forge_agent.sandbox import configured_docker_sandbox
from forge_agent.tools import (
    ApplyPatchTool,
    GitDiffTool,
    GitStatusTool,
    ListFilesTool,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    ToolRegistry,
    Workspace,
)
from forge_agent.tracing import EventBus, JsonlTraceSink, Redactor, RunTracer

EvalCategory = Literal[
    "bug_fix",
    "add_test",
    "feature",
    "refactor",
    "readonly",
    "safety",
]
HiddenCheckKind = Literal[
    "exists",
    "absent",
    "contains",
    "not_contains",
    "unchanged",
]


class EvalModel(BaseModel):
    """Strict immutable base for evaluation artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HiddenCheck(EvalModel):
    """Acceptance condition never included in the agent prompt."""

    path: str = Field(min_length=1)
    kind: HiddenCheckKind
    value: str | None = None


class EvalConstraints(EvalModel):
    """Task-specific change limits."""

    max_changed_files: int = Field(default=5, ge=0)
    forbidden_paths: tuple[str, ...] = (".env", ".git")


class EvalTask(EvalModel):
    """Versioned task snapshot and separate public/hidden acceptance."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    category: EvalCategory
    prompt: str = Field(min_length=1)
    initial_files: dict[str, str]
    public_test_command: tuple[str, ...] = Field(min_length=1)
    hidden_checks: tuple[HiddenCheck, ...] = Field(min_length=1)
    constraints: EvalConstraints = Field(default_factory=EvalConstraints)
    reference_files: dict[str, str] = Field(default_factory=dict)

    def public_input(self) -> EvalTaskInput:
        """Remove hidden checks, snapshots and reference answers for the agent."""
        return EvalTaskInput(
            id=self.id,
            category=self.category,
            prompt=self.prompt,
            public_test_command=self.public_test_command,
            constraints=self.constraints,
        )


class EvalTaskInput(EvalModel):
    """Only task information an evaluated agent is allowed to receive."""

    id: str
    category: EvalCategory
    prompt: str
    public_test_command: tuple[str, ...]
    constraints: EvalConstraints


class EvalExecution(EvalModel):
    """Normalized execution returned by real or reference agents."""

    status: str
    stop_reason: str
    duration_seconds: float = Field(ge=0)
    steps: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    known_cost: Decimal = Field(default=Decimal("0"), ge=0)
    cost_complete: bool = True
    trace_path: str | None = None

    @classmethod
    def from_run_result(cls, result: RunResult, *, trace_path: Path | None) -> EvalExecution:
        return cls(
            status=result.status.value,
            stop_reason=result.stop_reason.value,
            duration_seconds=float(result.metrics.duration_seconds),
            steps=result.metrics.steps,
            tool_calls=result.metrics.tool_calls,
            total_tokens=result.metrics.total_tokens,
            known_cost=result.metrics.known_cost,
            cost_complete=result.metrics.cost_complete,
            trace_path=str(trace_path) if trace_path is not None else None,
        )


class EvalExecutor(Protocol):
    """One agent implementation evaluated inside a fresh workspace."""

    async def execute(self, task: EvalTaskInput, workspace: Path) -> EvalExecution:
        """Run the task without reading hidden checks."""


class ReferenceExecutor:
    """Harness self-test that applies catalog reference files directly."""

    def __init__(self, tasks: Iterable[EvalTask]) -> None:
        self._reference_files = {task.id: dict(task.reference_files) for task in tasks}

    async def execute(self, task: EvalTaskInput, workspace: Path) -> EvalExecution:
        started = time.monotonic()
        for relative, content in self._reference_files[task.id].items():
            path = _safe_task_path(workspace, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        await asyncio.sleep(0)
        return EvalExecution(
            status="completed",
            stop_reason="reference_applied",
            duration_seconds=time.monotonic() - started,
        )


class RuntimeEvalExecutor:
    """Run the real ForgeAgent stack while keeping hidden checks outside it."""

    def __init__(self, settings: ForgeSettings, trace_directory: Path) -> None:
        self._settings = settings
        self._trace_directory = trace_directory

    async def execute(
        self,
        task: EvalTaskInput,
        workspace_path: Path,
    ) -> EvalExecution:
        workspace_settings = self._settings.workspace.model_copy(
            update={"allowed_root": workspace_path.resolve()}
        )
        settings = self._settings.model_copy(update={"workspace": workspace_settings})
        public_command = " ".join(task.public_test_command)
        config = settings.to_run_config(
            f"{task.prompt}\nPublic verification command: {public_command}"
        )
        trace_path = self._trace_directory / f"{task.id}-{config.run_id}.jsonl"
        tracer = RunTracer(
            run_id=config.run_id,
            bus=EventBus((JsonlTraceSink(trace_path),)),
            redactor=Redactor(
                known_secrets=(
                    settings.model.api_key.get_secret_value()
                    if settings.model.api_key is not None
                    else "",
                )
            ),
        )
        workspace = Workspace(
            workspace_path,
            respect_gitignore=settings.workspace.respect_gitignore,
        )
        sandbox = configured_docker_sandbox(settings, workspace=workspace_path)
        registry = ToolRegistry(
            (
                ListFilesTool(workspace),
                ReadFileTool(workspace),
                SearchCodeTool(workspace),
                ApplyPatchTool(workspace),
                GitDiffTool(workspace),
                GitStatusTool(workspace),
                RunCommandTool(
                    workspace,
                    approval_mode="never",
                    tracer=tracer,
                    sandbox=sandbox,
                    execution_mode=settings.execution.backend,
                ),
            )
        )
        provider = create_model_provider(settings.model)
        runtime = AgentRuntime(
            provider=provider,
            tools=registry,
            tracer=tracer,
            context_manager=ContextManager(
                workspace,
                max_tokens=settings.context.max_tokens,
                compression_ratio=float(settings.context.compression_ratio),
                repeated_call_limit=settings.context.repeated_call_limit,
                preserve_recent_messages=settings.context.preserve_recent_messages,
            ),
        )
        try:
            result = await runtime.run(config)
        finally:
            await provider.aclose()
        return EvalExecution.from_run_result(result, trace_path=trace_path)


class EvalRecord(EvalModel):
    """One scored task repetition."""

    task_id: str
    category: EvalCategory
    repetition: int = Field(gt=0)
    success: bool
    public_test_passed: bool
    hidden_checks_passed: int = Field(ge=0)
    hidden_checks_total: int = Field(gt=0)
    constraint_violations: tuple[str, ...] = ()
    failure_type: str | None = None
    duration_seconds: float = Field(ge=0)
    steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    known_cost: Decimal = Field(ge=0)
    cost_complete: bool
    changed_files: int = Field(ge=0)
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    agent_generated_test_files: int = Field(ge=0)
    trace_path: str | None = None


class EvalSummary(EvalModel):
    """Aggregate metrics suitable for regression comparison."""

    runs: int = Field(gt=0)
    successes: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    mean_duration_seconds: float = Field(ge=0)
    p95_duration_seconds: float = Field(ge=0)
    mean_steps: float = Field(ge=0)
    total_tokens: int = Field(ge=0)
    known_cost: Decimal = Field(ge=0)
    mean_changed_files: float = Field(ge=0)
    failure_types: dict[str, int]


class EvalReport(EvalModel):
    """Complete machine-readable evaluation report."""

    schema_version: Literal["1.0"] = "1.0"
    created_at: datetime
    version: str
    git_commit: str
    model: str
    run_metadata: dict[str, JsonValue] = Field(default_factory=dict)
    repetitions: int = Field(gt=0)
    task_count: int = Field(gt=0)
    summary: EvalSummary
    baseline_delta: dict[str, float] = Field(default_factory=dict)
    records: tuple[EvalRecord, ...]


class EvaluationRunner:
    """Materialize, execute and score each task in an isolated temp snapshot."""

    def __init__(
        self,
        *,
        tasks: Iterable[EvalTask],
        executor: EvalExecutor,
        version: str,
        git_commit: str,
        model: str,
        run_metadata: dict[str, JsonValue] | None = None,
    ) -> None:
        self._tasks = tuple(tasks)
        if not self._tasks:
            raise ValueError("evaluation task catalog cannot be empty")
        self._executor = executor
        self._version = version
        self._git_commit = git_commit
        self._model = model
        self._run_metadata = dict(run_metadata or {})

    async def run(
        self,
        *,
        repetitions: int = 1,
        baseline: EvalReport | None = None,
    ) -> EvalReport:
        if repetitions <= 0:
            raise ValueError("repetitions must be greater than zero")
        records: list[EvalRecord] = []
        for task in self._tasks:
            for repetition in range(1, repetitions + 1):
                records.append(await self._run_once(task, repetition))
        summary = summarize(records)
        delta: dict[str, float] = {}
        if baseline is not None:
            delta = {
                "success_rate": summary.success_rate - baseline.summary.success_rate,
                "mean_duration_seconds": (
                    summary.mean_duration_seconds - baseline.summary.mean_duration_seconds
                ),
                "mean_steps": summary.mean_steps - baseline.summary.mean_steps,
                "total_tokens": float(summary.total_tokens - baseline.summary.total_tokens),
                "p95_duration_seconds": (
                    summary.p95_duration_seconds - baseline.summary.p95_duration_seconds
                ),
                "known_cost": float(summary.known_cost - baseline.summary.known_cost),
                "mean_changed_files": (
                    summary.mean_changed_files - baseline.summary.mean_changed_files
                ),
            }
        return EvalReport(
            created_at=datetime.now(UTC),
            version=self._version,
            git_commit=self._git_commit,
            model=self._model,
            run_metadata=self._run_metadata,
            repetitions=repetitions,
            task_count=len(self._tasks),
            summary=summary,
            baseline_delta=delta,
            records=tuple(records),
        )

    async def _run_once(self, task: EvalTask, repetition: int) -> EvalRecord:
        with tempfile.TemporaryDirectory(prefix=f"forge-eval-{task.id}-") as directory:
            workspace = Path(directory)
            _materialize(workspace, task.initial_files)
            before = _snapshot(workspace)
            try:
                execution = await self._executor.execute(task.public_input(), workspace)
            except Exception as error:
                execution = EvalExecution(
                    status="failed",
                    stop_reason=f"executor_{type(error).__name__}",
                    duration_seconds=0,
                )
            public_passed = _run_public_test(task, workspace)
            hidden_passed = sum(
                _check_hidden(check, workspace, before) for check in task.hidden_checks
            )
            after = _snapshot(workspace)
            diff = _diff_metrics(before, after)
            violations = _constraint_violations(task, workspace, diff.changed_paths)
            success = (
                execution.status == "completed"
                and public_passed
                and hidden_passed == len(task.hidden_checks)
                and not violations
            )
            failure_type = None
            if not success:
                if execution.status != "completed":
                    failure_type = execution.stop_reason
                elif not public_passed:
                    failure_type = "public_test_failed"
                elif hidden_passed != len(task.hidden_checks):
                    failure_type = "hidden_check_failed"
                else:
                    failure_type = "constraint_failed"
            generated_tests = sum(
                path.startswith(("test_", "tests/")) and path not in before for path in after
            )
            return EvalRecord(
                task_id=task.id,
                category=task.category,
                repetition=repetition,
                success=success,
                public_test_passed=public_passed,
                hidden_checks_passed=hidden_passed,
                hidden_checks_total=len(task.hidden_checks),
                constraint_violations=tuple(violations),
                failure_type=failure_type,
                duration_seconds=execution.duration_seconds,
                steps=execution.steps,
                tool_calls=execution.tool_calls,
                total_tokens=execution.total_tokens,
                known_cost=execution.known_cost,
                cost_complete=execution.cost_complete,
                changed_files=len(diff.changed_paths),
                added_lines=diff.added_lines,
                deleted_lines=diff.deleted_lines,
                agent_generated_test_files=generated_tests,
                trace_path=execution.trace_path,
            )


@dataclass(frozen=True, slots=True)
class _DiffMetrics:
    changed_paths: tuple[str, ...]
    added_lines: int
    deleted_lines: int


def summarize(records: list[EvalRecord]) -> EvalSummary:
    """Aggregate records with a deterministic nearest-rank P95."""
    if not records:
        raise ValueError("cannot summarize no records")
    durations = sorted(record.duration_seconds for record in records)
    p95_index = max(math.ceil(0.95 * len(durations)) - 1, 0)
    failures = Counter(record.failure_type for record in records if record.failure_type is not None)
    return EvalSummary(
        runs=len(records),
        successes=sum(record.success for record in records),
        success_rate=sum(record.success for record in records) / len(records),
        mean_duration_seconds=statistics.fmean(durations),
        p95_duration_seconds=durations[p95_index],
        mean_steps=statistics.fmean(record.steps for record in records),
        total_tokens=sum(record.total_tokens for record in records),
        known_cost=sum((record.known_cost for record in records), Decimal("0")),
        mean_changed_files=statistics.fmean(record.changed_files for record in records),
        failure_types=dict(sorted(failures.items())),
    )


def write_report(report: EvalReport, output_directory: Path) -> tuple[Path, Path]:
    """Write matching JSON and Markdown summaries."""
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "evaluation.json"
    markdown_path = output_directory / "evaluation.md"
    json_path.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def load_report(path: Path) -> EvalReport:
    """Load a previous JSON report for baseline comparison."""
    return EvalReport.model_validate_json(path.read_text(encoding="utf-8"))


def render_markdown(report: EvalReport) -> str:
    """Render the minimum evaluation report fields."""
    summary = report.summary
    lines = [
        "# ForgeAgent Evaluation Report",
        "",
        f"- Version: `{report.version}`",
        f"- Git commit: `{report.git_commit}`",
        f"- Model: `{report.model}`",
        f"- Tasks: {report.task_count}",
        f"- Repetitions: {report.repetitions}",
        f"- Success: {summary.successes}/{summary.runs} ({summary.success_rate:.1%})",
        f"- Mean / P95 duration: {summary.mean_duration_seconds:.3f}s / "
        f"{summary.p95_duration_seconds:.3f}s",
        f"- Mean steps: {summary.mean_steps:.2f}",
        f"- Total tokens: {summary.total_tokens}",
        f"- Known cost: ${summary.known_cost}",
        f"- Mean changed files: {summary.mean_changed_files:.2f}",
        "",
    ]
    if report.run_metadata:
        lines.extend(
            (
                "## Run metadata",
                "",
                *(f"- {key}: `{value}`" for key, value in sorted(report.run_metadata.items())),
                "",
            )
        )
    lines.extend(("## Failure types", ""))
    if summary.failure_types:
        lines.extend(f"- {failure}: {count}" for failure, count in summary.failure_types.items())
    else:
        lines.append("- None")
    lines.extend(("", "## Baseline delta", ""))
    if report.baseline_delta:
        lines.extend(f"- {metric}: {value:+.4f}" for metric, value in report.baseline_delta.items())
    else:
        lines.append("- No baseline supplied")
    return "\n".join(lines) + "\n"


def _materialize(workspace: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = _safe_task_path(workspace, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _safe_task_path(workspace: Path, relative: str) -> Path:
    candidate = (workspace / relative).resolve(strict=False)
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        raise ValueError(f"task path escapes workspace: {relative}") from None
    return candidate


def _snapshot(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        try:
            snapshot[path.relative_to(workspace).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
    return snapshot


def _run_public_test(task: EvalTask, workspace: Path) -> bool:
    command = [
        sys.executable if argument == "{python}" else argument
        for argument in task.public_test_command
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    }
    environment["PYTHONUTF8"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=workspace,
            check=False,
            capture_output=True,
            timeout=30,
            env=environment,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _check_hidden(
    check: HiddenCheck,
    workspace: Path,
    initial: dict[str, str],
) -> bool:
    path = (workspace / check.path).resolve(strict=False)
    exists = path.exists()
    if check.kind == "exists":
        return exists
    if check.kind == "absent":
        return not exists
    try:
        relative = path.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return False
    if check.kind == "unchanged":
        return exists and path.read_text(encoding="utf-8") == initial.get(relative)
    if not exists or not path.is_file() or check.value is None:
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    if check.kind == "contains":
        return check.value in content
    if check.kind == "not_contains":
        return check.value not in content
    return False


def _diff_metrics(
    before: dict[str, str],
    after: dict[str, str],
) -> _DiffMetrics:
    changed = tuple(
        path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)
    )
    added = 0
    deleted = 0
    for path in changed:
        matcher = difflib.SequenceMatcher(
            a=before.get(path, "").splitlines(),
            b=after.get(path, "").splitlines(),
        )
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if operation in {"replace", "delete"}:
                deleted += old_end - old_start
            if operation in {"replace", "insert"}:
                added += new_end - new_start
    return _DiffMetrics(changed_paths=changed, added_lines=added, deleted_lines=deleted)


def _constraint_violations(
    task: EvalTask,
    workspace: Path,
    changed_paths: tuple[str, ...],
) -> list[str]:
    violations: list[str] = []
    if len(changed_paths) > task.constraints.max_changed_files:
        violations.append(
            f"changed_files={len(changed_paths)} exceeds {task.constraints.max_changed_files}"
        )
    for forbidden in task.constraints.forbidden_paths:
        path = (workspace / forbidden).resolve(strict=False)
        if path.exists() and forbidden in changed_paths:
            violations.append(f"forbidden path changed: {forbidden}")
    return violations
