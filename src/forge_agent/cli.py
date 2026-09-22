"""Command-line interface for configuring and running ForgeAgent."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import typer
from pydantic import JsonValue
from rich.console import Console
from rich.table import Table

from forge_agent import __version__
from forge_agent.config import ConfigurationError, ForgeSettings, ModelSettings, load_settings
from forge_agent.context import ContextManager
from forge_agent.doctor import DiagnosticStatus, has_failures, run_diagnostics
from forge_agent.domain import RunConfig, RunResult, RunStatus
from forge_agent.engine import TurnResult
from forge_agent.eval_catalog import build_task_catalog
from forge_agent.evaluation import (
    EvalExecutor,
    EvaluationRunner,
    ReferenceExecutor,
    RuntimeEvalExecutor,
    load_report,
    write_report,
)
from forge_agent.models import ModelProvider, create_model_provider
from forge_agent.policy import CommandReview
from forge_agent.prompts import PROMPT_VERSION
from forge_agent.rlm import RLMApplication
from forge_agent.runtime import AgentRuntime
from forge_agent.sandbox import configured_docker_sandbox
from forge_agent.session import SessionStore
from forge_agent.tools import (
    ApplyPatchTool,
    GitDiffTool,
    GitStatusTool,
    ListFilesTool,
    PatchReview,
    ReadFileTool,
    RunCommandTool,
    SearchCodeTool,
    ToolRegistry,
    Workspace,
)
from forge_agent.tracing import (
    ConsoleEventSink,
    EventBus,
    JsonlTraceSink,
    Redactor,
    RunTracer,
)

EXIT_FAILURE = 1
EXIT_CONFIGURATION = 2
EXIT_STOPPED = 3
EXIT_CANCELLED = 130

console = Console()
app = typer.Typer(
    name="forge",
    help="A transparent, security-conscious coding agent for local repositories.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate ForgeAgent configuration.")
tools_app = typer.Typer(help="Inspect the tools available to the agent.")
eval_app = typer.Typer(help="Run repeatable ForgeAgent evaluation tasks.")
rlm_app = typer.Typer(help="Run explicitly trusted persistent RLM sessions.")
session_app = typer.Typer(help="Inspect and explicitly repair persistent sessions.")
app.add_typer(config_app, name="config")
app.add_typer(tools_app, name="tools")
app.add_typer(eval_app, name="eval")
app.add_typer(rlm_app, name="rlm")
app.add_typer(session_app, name="session")


def version_callback(value: bool) -> None:
    """Print the package version and exit."""
    if value:
        typer.echo(f"forge-agent {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=version_callback,
            is_eager=True,
            help="Show the installed ForgeAgent version and exit.",
        ),
    ] = None,
) -> None:
    """Run ForgeAgent commands."""


@app.command("run")
def run_command(
    task: Annotated[str, typer.Argument(help="Natural-language repository task.")],
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Repository directory available to bounded agent tools.",
        ),
    ] = Path("."),
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Optional forge.toml path."),
    ] = None,
    trace_path: Annotated[
        Path | None,
        typer.Option(
            "--trace",
            help="JSONL trace path; defaults to WORKSPACE/.forge/traces/RUN_ID.jsonl.",
        ),
    ] = None,
) -> None:
    """Run the coding agent against one bounded workspace."""
    settings = _load_cli_settings(workspace=workspace, config_path=config_path)
    config = settings.to_run_config(task)
    resolved_trace = _resolve_trace_path(trace_path, config)
    _show_run_configuration(settings, config, resolved_trace)

    try:
        provider = _create_live_provider(settings.model)
    except ConfigurationError as error:
        _configuration_failure(error)

    tracer = RunTracer(
        run_id=config.run_id,
        bus=EventBus(
            (
                ConsoleEventSink(console),
                JsonlTraceSink(resolved_trace),
            )
        ),
        redactor=Redactor(
            known_secrets=(
                settings.model.api_key.get_secret_value()
                if settings.model.api_key is not None
                else "",
            )
        ),
    )
    registry = _build_registry(settings, tracer=tracer)
    context_workspace = Workspace(
        settings.workspace.allowed_root,
        respect_gitignore=settings.workspace.respect_gitignore,
    )
    runtime = AgentRuntime(
        provider=provider,
        tools=registry,
        tracer=tracer,
        context_manager=ContextManager(
            context_workspace,
            max_tokens=settings.context.max_tokens,
            compression_ratio=float(settings.context.compression_ratio),
            repeated_call_limit=settings.context.repeated_call_limit,
            preserve_recent_messages=settings.context.preserve_recent_messages,
        ),
    )
    try:
        result = asyncio.run(_run_and_close(runtime, config, provider))
    except KeyboardInterrupt:
        console.print("[yellow]Run cancelled by user.[/yellow]")
        raise typer.Exit(code=EXIT_CANCELLED) from None

    _show_run_result(result, resolved_trace, tracer)
    if result.status is RunStatus.COMPLETED:
        return
    if result.status is RunStatus.STOPPED:
        raise typer.Exit(code=EXIT_STOPPED)
    if result.status is RunStatus.CANCELLED:
        raise typer.Exit(code=EXIT_CANCELLED)
    raise typer.Exit(code=EXIT_FAILURE)


@rlm_app.command("run")
def rlm_run_command(
    task: Annotated[str, typer.Argument(help="Task for the persistent RLM session.")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    trust_workspace: Annotated[
        bool,
        typer.Option(
            "--trust-workspace",
            help="Confirm that a user-permission IPython kernel may run workspace code.",
        ),
    ] = False,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Start a durable trusted RLM session; safe forge run remains unchanged."""
    _require_workspace_trust(trust_workspace)
    settings = _load_cli_settings(workspace=workspace, config_path=config_path)
    try:
        provider = _create_live_provider(settings.model)
    except ConfigurationError as error:
        _configuration_failure(error)
    store = SessionStore.create(settings.workspace.allowed_root)
    application = RLMApplication(
        settings=settings,
        provider=provider,
        store=store,
        registry_factory=lambda: _build_registry(settings),
    )
    console.print(
        "[bold yellow]Trusted RLM mode:[/bold yellow] the IPython kernel inherits your "
        "current user permissions and is not a sandbox."
    )
    console.print(f"Session: {store.header.session_id}")
    try:
        results = asyncio.run(_run_rlm_application(application, task=task))
    except KeyboardInterrupt:
        console.print("[yellow]RLM session cancelled.[/yellow]")
        raise typer.Exit(code=EXIT_CANCELLED) from None
    _show_rlm_results(results)


@rlm_app.command("resume")
def rlm_resume_command(
    session_id: Annotated[str, typer.Argument(help="Root session UUID.")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    trust_workspace: Annotated[bool, typer.Option("--trust-workspace")] = False,
    config_path: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Resume durable unconsumed inputs without replaying uncertain side effects."""
    _require_workspace_trust(trust_workspace)
    settings = _load_cli_settings(workspace=workspace, config_path=config_path)
    try:
        provider = _create_live_provider(settings.model)
        store = SessionStore.open(settings.workspace.allowed_root, session_id)
    except (ConfigurationError, FileNotFoundError, ValueError) as error:
        _configuration_failure(ConfigurationError(str(error)))
    application = RLMApplication(
        settings=settings,
        provider=provider,
        store=store,
        registry_factory=lambda: _build_registry(settings),
    )
    try:
        results = asyncio.run(_run_rlm_application(application))
    except KeyboardInterrupt:
        raise typer.Exit(code=EXIT_CANCELLED) from None
    console.print(f"Session: {store.header.session_id}")
    _show_rlm_results(results)


@session_app.command("list")
def session_list_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
) -> None:
    """List root sessions without loading model credentials."""
    root = workspace.resolve(strict=False) / ".forge" / "sessions"
    table = Table(title="ForgeAgent sessions")
    table.add_column("Session", style="cyan")
    table.add_column("Generation")
    table.add_column("Tail")
    if root.is_dir():
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            try:
                store = SessionStore.open(workspace, directory.name)
                tail = store.inspect_tail()
            except (OSError, ValueError):
                continue
            table.add_row(
                str(store.header.session_id),
                str(store.header.generation),
                "valid" if tail.valid else f"repairable at {tail.last_good_offset}",
            )
    console.print(table)


@session_app.command("show")
def session_show_command(
    session_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
) -> None:
    """Show journal metadata and entry counts; unknown types are preserved."""
    try:
        store = SessionStore.open(workspace, session_id)
        entries = store.replay()
    except (OSError, ValueError) as error:
        console.print(f"[red]Unable to open session:[/red] {error}")
        raise typer.Exit(code=EXIT_CONFIGURATION) from None
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.type] = counts.get(entry.type, 0) + 1
    table = Table(title=f"Session {store.header.session_id}")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Root", str(store.header.root_session_id))
    table.add_row("Parent", str(store.header.parent_id or "(none)"))
    table.add_row("Generation", str(store.header.generation))
    table.add_row("Workspace", store.header.workspace)
    table.add_row("Tail", store.inspect_tail().model_dump_json())
    for kind, count in sorted(counts.items()):
        table.add_row(f"Entries: {kind}", str(count))
    console.print(table)


@session_app.command("repair")
def session_repair_command(
    session_id: Annotated[str, typer.Argument()],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path("."),
    truncate_tail: Annotated[
        bool,
        typer.Option(
            "--truncate-tail",
            help="Explicitly truncate only the detected incomplete/invalid tail.",
        ),
    ] = False,
) -> None:
    """Inspect a tail by default; mutate it only with --truncate-tail."""
    try:
        store = SessionStore.open(workspace, session_id)
        before = store.inspect_tail()
        after = store.repair_tail(truncate_tail=truncate_tail)
    except (OSError, ValueError) as error:
        console.print(f"[red]Unable to repair session:[/red] {error}")
        raise typer.Exit(code=EXIT_CONFIGURATION) from None
    console.print(f"Before: {before.model_dump_json()}")
    console.print(f"After:  {after.model_dump_json()}")
    if not truncate_tail and not before.valid:
        console.print("[yellow]No file was changed; rerun with --truncate-tail.[/yellow]")


@app.command("doctor")
def doctor_command(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Workspace to diagnose."),
    ] = Path("."),
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Optional forge.toml path."),
    ] = None,
) -> None:
    """Run read-only local diagnostics without contacting a model service."""
    settings = _load_cli_settings(workspace=workspace, config_path=config_path)
    checks = run_diagnostics(settings)
    table = Table(title="ForgeAgent doctor")
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Detail")
    styles = {
        DiagnosticStatus.PASS: "green",
        DiagnosticStatus.WARN: "yellow",
        DiagnosticStatus.FAIL: "red",
    }
    for check in checks:
        style = styles[check.status]
        table.add_row(check.name, f"[{style}]{check.status.value}[/{style}]", check.detail)
    console.print(table)
    if has_failures(checks):
        raise typer.Exit(code=EXIT_CONFIGURATION)


@config_app.command("check")
def check_config(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Workspace used to resolve configuration."),
    ] = Path("."),
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Optional forge.toml path."),
    ] = None,
    require_model: Annotated[
        bool,
        typer.Option(
            "--require-model/--allow-missing-model",
            help="Require credentials for a live model request.",
        ),
    ] = True,
) -> None:
    """Validate layered settings without making a network request."""
    settings = _load_cli_settings(workspace=workspace, config_path=config_path)
    if require_model:
        try:
            settings.model.require_live_configuration()
        except ConfigurationError as error:
            _configuration_failure(error)
    table = Table(title="ForgeAgent configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    table.add_row("Workspace", str(settings.workspace.allowed_root))
    table.add_row("Provider", settings.model.provider)
    table.add_row("Base URL", str(settings.model.base_url or "(not configured)"))
    table.add_row("Model", settings.model.name or "(not configured)")
    table.add_row("API Key", "configured (redacted)" if settings.model.api_key else "missing")
    table.add_row("Max steps", str(settings.agent.max_steps))
    table.add_row("Max tool calls", str(settings.agent.max_tool_calls))
    table.add_row("Max tokens", str(settings.agent.max_tokens or "(not limited)"))
    table.add_row("Max cost (USD)", str(settings.agent.max_cost_usd or "(not limited)"))
    table.add_row("Timeout (seconds)", str(settings.agent.timeout_seconds))
    table.add_row("Context tokens", str(settings.context.max_tokens))
    table.add_row("Patch approval", settings.patches.approval)
    table.add_row("Execution backend", settings.execution.backend)
    if settings.execution.backend in {"auto", "docker"}:
        table.add_row("Docker image", settings.execution.docker_image)
    console.print(table)
    if not require_model and settings.model.missing_live_fields():
        console.print(
            "[yellow]Core configuration is valid; live model settings are incomplete.[/yellow]"
        )
    else:
        console.print("[green]Configuration is valid. No network request was made.[/green]")


@tools_app.command("list")
def list_tools(
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "-w", help="Workspace used to build bounded tools."),
    ] = Path("."),
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Optional forge.toml path."),
    ] = None,
) -> None:
    """List deterministic tool schemas without model credentials."""
    settings = _load_cli_settings(workspace=workspace, config_path=config_path)
    registry = _build_registry(settings)
    table = Table(title="ForgeAgent tools")
    table.add_column("Name", style="cyan")
    table.add_column("Description")
    table.add_column("Parameters")
    for schema in registry.schemas:
        raw_properties = schema.parameters.get("properties")
        parameter_names = (
            ", ".join(sorted(raw_properties)) if isinstance(raw_properties, dict) else "(none)"
        )
        table.add_row(schema.name, schema.description, parameter_names)
    console.print(table)
    console.print(f"Workspace: {settings.workspace.allowed_root}")


@eval_app.command("list")
def list_evaluations() -> None:
    """List the built-in versioned evaluation catalog."""
    tasks = build_task_catalog()
    table = Table(title="ForgeAgent evaluation tasks")
    table.add_column("ID", style="cyan")
    table.add_column("Category")
    table.add_column("Prompt")
    for task in tasks:
        table.add_row(task.id, task.category, task.prompt)
    console.print(table)
    console.print(f"Total: {len(tasks)} tasks")


@eval_app.command("run")
def run_evaluations(
    mode: Annotated[
        Literal["reference", "live"],
        typer.Option(help="reference validates the harness; live calls the configured model."),
    ] = "reference",
    repetitions: Annotated[
        int,
        typer.Option(min=1, max=20, help="Independent repetitions per task."),
    ] = 1,
    output: Annotated[
        Path,
        typer.Option(help="Directory for JSON, Markdown and failure traces."),
    ] = Path("reports/evaluation"),
    baseline: Annotated[
        Path | None,
        typer.Option(help="Previous evaluation.json for regression deltas."),
    ] = None,
) -> None:
    """Run all 20 tasks from clean temporary snapshots."""
    tasks = build_task_catalog()
    baseline_report = load_report(baseline) if baseline is not None else None
    executor: EvalExecutor
    run_metadata: dict[str, JsonValue]
    if mode == "reference":
        executor = ReferenceExecutor(tasks)
        model_name = "reference-harness (not a model score)"
        run_metadata = {
            "mode": "reference",
            "prompt_version": PROMPT_VERSION,
        }
    else:
        settings = _load_cli_settings(workspace=Path("."), config_path=None)
        try:
            settings.model.require_live_configuration()
        except ConfigurationError as error:
            _configuration_failure(error)
        executor = RuntimeEvalExecutor(settings, output / "traces")
        model_name = settings.model.name or "(not configured)"
        run_metadata = {
            "mode": "live",
            "provider": settings.model.provider,
            "prompt_version": PROMPT_VERSION,
            "max_steps": settings.agent.max_steps,
            "max_tool_calls": settings.agent.max_tool_calls,
            "max_output_tokens": settings.model.max_output_tokens,
            "pricing_configured": (settings.model.input_cost_per_million is not None),
        }
    runner = EvaluationRunner(
        tasks=tasks,
        executor=executor,
        version=__version__,
        git_commit=_git_commit(),
        model=model_name,
        run_metadata=run_metadata,
    )
    report = asyncio.run(runner.run(repetitions=repetitions, baseline=baseline_report))
    json_path, markdown_path = write_report(report, output)
    console.print(
        f"Evaluation complete: {report.summary.successes}/{report.summary.runs} successful"
    )
    console.print(f"JSON: {json_path}")
    console.print(f"Markdown: {markdown_path}")


async def _run_and_close(
    runtime: AgentRuntime,
    config: RunConfig,
    provider: ModelProvider,
) -> RunResult:
    """Run once and always release provider-owned HTTP resources."""
    try:
        return await runtime.run(config)
    finally:
        await provider.aclose()


async def _run_rlm_application(
    application: RLMApplication,
    *,
    task: str | None = None,
) -> tuple[TurnResult, ...]:
    try:
        if task is not None:
            return await application.submit_and_run(task)
        return await application.resume()
    finally:
        await application.shutdown()


def _require_workspace_trust(confirmed: bool) -> None:
    if confirmed:
        return
    console.print(
        "[red]RLM mode refused.[/red] Pass --trust-workspace only after reviewing the "
        "workspace. Its persistent IPython kernel is not sandboxed and inherits your user "
        "permissions."
    )
    raise typer.Exit(code=EXIT_CONFIGURATION)


def _show_rlm_results(results: tuple[object, ...]) -> None:
    if not results:
        console.print("No unconsumed input was pending; no side effect was replayed.")
        return
    for result in results:
        final_text = getattr(result, "final_text", None)
        status = getattr(getattr(result, "status", None), "value", "unknown")
        stop_reason = getattr(getattr(result, "stop_reason", None), "value", "unknown")
        if final_text:
            console.print("\n[bold green]Final answer[/bold green]")
            console.print(final_text)
        console.print(f"Turn: status={status}, stop_reason={stop_reason}")


def _create_live_provider(settings: ModelSettings) -> ModelProvider:
    """Indirection point replaced by deterministic Fake Providers in CLI tests."""
    return create_model_provider(settings)


def _load_cli_settings(
    *,
    workspace: Path,
    config_path: Path | None,
) -> ForgeSettings:
    try:
        return load_settings(
            config_path=config_path,
            cli_overrides={"workspace": {"allowed_root": workspace}},
        )
    except ConfigurationError as error:
        _configuration_failure(error)


def _configuration_failure(error: ConfigurationError) -> NoReturn:
    console.print(f"[red]Configuration error:[/red] {error}")
    if "FORGE_MODEL_" in str(error):
        console.print(
            "Set FORGE_MODEL_PROVIDER, FORGE_MODEL_BASE_URL, "
            "FORGE_MODEL_NAME and (except for Ollama) FORGE_MODEL_API_KEY, then run "
            "'forge config check'."
        )
        console.print("See MODEL_CONFIGURATION.md for PowerShell examples.")
    raise typer.Exit(code=EXIT_CONFIGURATION)


def _build_registry(
    settings: ForgeSettings,
    *,
    tracer: RunTracer | None = None,
) -> ToolRegistry:
    workspace = Workspace(
        settings.workspace.allowed_root,
        respect_gitignore=settings.workspace.respect_gitignore,
    )
    sandbox = configured_docker_sandbox(settings)
    return ToolRegistry(
        (
            ListFilesTool(workspace),
            ReadFileTool(workspace),
            SearchCodeTool(workspace),
            ApplyPatchTool(
                workspace,
                approval_handler=_approve_patch,
                approval_mode=settings.patches.approval,
                tracer=tracer,
            ),
            GitDiffTool(workspace),
            GitStatusTool(workspace),
            RunCommandTool(
                workspace,
                approval_handler=_approve_command,
                approval_mode=settings.commands.approval,
                tracer=tracer,
                sandbox=sandbox,
                execution_mode=settings.execution.backend,
            ),
        )
    )


def _approve_command(review: CommandReview) -> bool:
    console.print("\n[bold yellow]Command approval required[/bold yellow]")
    console.print(f"Risk: {review.risk.value}")
    console.print(f"Reason: {review.reason}")
    console.print(f"Execution backend: {review.execution_backend}")
    console.print(f"Routing: {review.routing_reason}")
    console.print(f"Working directory: {review.request.cwd}")
    console.print("Command: " + " ".join((review.request.executable, *review.request.args)))
    return typer.confirm("Allow this command for this invocation?", default=False)


def _approve_patch(review: PatchReview) -> bool:
    console.print("\n[bold yellow]Patch approval required[/bold yellow]")
    console.print(
        f"Files: {len(review.changed_files)}; +{review.added_lines}/-{review.deleted_lines} lines"
    )
    if review.preexisting_changes:
        console.print("[yellow]Targets with pre-existing changes:[/yellow]")
        for path in review.preexisting_changes:
            console.print(f"- {path}")
    console.print(review.diff)
    return typer.confirm("Apply this patch for this invocation?", default=False)


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _resolve_trace_path(path: Path | None, config: RunConfig) -> Path:
    candidate = path or config.workspace / ".forge" / "traces" / f"{config.run_id}.jsonl"
    return candidate.expanduser().resolve(strict=False)


def _show_run_configuration(
    settings: ForgeSettings,
    config: RunConfig,
    trace_path: Path,
) -> None:
    table = Table(title="ForgeAgent run")
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    table.add_row("Workspace", str(config.workspace))
    table.add_row("Provider", settings.model.provider)
    table.add_row("Model", config.model_name or "(not configured)")
    table.add_row("Max steps", str(config.budget.max_steps))
    table.add_row("Max tool calls", str(config.budget.max_tool_calls))
    table.add_row("Max tokens", str(config.budget.max_tokens or "(not limited)"))
    table.add_row("Max cost (USD)", str(config.budget.max_cost or "(not limited)"))
    table.add_row("Timeout (seconds)", str(config.budget.timeout_seconds))
    table.add_row("Trace", str(trace_path))
    console.print(table)


def _show_run_result(
    result: RunResult,
    trace_path: Path,
    tracer: RunTracer,
) -> None:
    if result.final_text is not None:
        console.print("\n[bold green]Final answer[/bold green]")
        console.print(result.final_text)
    table = Table(title="Run result")
    table.add_column("Metric", style="cyan")
    table.add_column("Value")
    table.add_row("Status", result.status.value)
    table.add_row("Stop reason", result.stop_reason.value)
    table.add_row("Steps", str(result.metrics.steps))
    table.add_row("Tool calls", str(result.metrics.tool_calls))
    table.add_row("Total tokens", str(result.metrics.total_tokens))
    table.add_row("Known cost (USD)", str(result.metrics.known_cost))
    table.add_row("Cost complete", str(result.metrics.cost_complete).lower())
    table.add_row("Duration (seconds)", f"{result.metrics.duration_seconds:.3f}")
    table.add_row("Context tokens", str(result.metrics.context_estimated_tokens))
    table.add_row("Context compressions", str(result.metrics.context_compressions))
    table.add_row("Deduplicated results", str(result.metrics.deduplicated_results))
    if result.error_type is not None:
        table.add_row("Error type", result.error_type)
    if result.error_message is not None:
        table.add_row("Error", result.error_message)
    table.add_row("Changed files", str(len(result.changed_files)))
    table.add_row("Trace", str(trace_path))
    console.print(table)
    if result.changed_files:
        console.print("\n[bold]Changed files[/bold]")
        for path in result.changed_files:
            console.print(f"- {path}")
    if result.diff_summary:
        console.print("\n[bold]Patch summary[/bold]")
        console.print(result.diff_summary)
    if result.plan is not None:
        console.print("\n[bold]Plan[/bold]")
        for step in result.plan.steps:
            console.print(f"- [{step.status.value}] {step.title}")
    if result.handoff_report:
        console.print("\n[bold yellow]Handoff report[/bold yellow]")
        console.print(result.handoff_report)
    if tracer.sink_failures:
        console.print(
            f"[yellow]Warning: {len(tracer.sink_failures)} trace sink failure(s) occurred.[/yellow]"
        )
