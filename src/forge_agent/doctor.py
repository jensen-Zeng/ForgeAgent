"""Read-only environment diagnostics for first-run support."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from enum import StrEnum
from importlib.util import find_spec

from pydantic import BaseModel, ConfigDict

from forge_agent.config import ForgeSettings
from forge_agent.processes import WINDOWS_CREATE_NO_WINDOW

CommandProbe = Callable[[list[str]], tuple[bool, str]]


class DiagnosticStatus(StrEnum):
    """Severity used by the doctor command."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class Diagnostic(BaseModel):
    """One machine-readable environment check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    status: DiagnosticStatus
    detail: str


def run_diagnostics(
    settings: ForgeSettings,
    *,
    command_probe: CommandProbe | None = None,
) -> tuple[Diagnostic, ...]:
    """Inspect the local environment without changing it or contacting providers."""
    probe = command_probe or _probe_command
    checks: list[Diagnostic] = []
    python_ok = sys.version_info >= (3, 12)
    checks.append(
        Diagnostic(
            name="Python",
            status=DiagnosticStatus.PASS if python_ok else DiagnosticStatus.FAIL,
            detail=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    rlm_modules = ("ipykernel", "jupyter_client", "psutil")
    missing_rlm = [name for name in rlm_modules if find_spec(name) is None]
    checks.append(
        Diagnostic(
            name="Trusted RLM dependencies",
            status=DiagnosticStatus.WARN if missing_rlm else DiagnosticStatus.PASS,
            detail=(
                "missing optional modules: " + ", ".join(missing_rlm)
                if missing_rlm
                else "available (kernel is started only with forge rlm)"
            ),
        )
    )
    in_virtualenv = sys.prefix != sys.base_prefix
    checks.append(
        Diagnostic(
            name="Virtual environment",
            status=DiagnosticStatus.PASS if in_virtualenv else DiagnosticStatus.WARN,
            detail="active" if in_virtualenv else "not active",
        )
    )
    workspace = settings.workspace.allowed_root
    workspace_ok = workspace.is_dir() and os.access(workspace, os.R_OK | os.W_OK)
    checks.append(
        Diagnostic(
            name="Workspace",
            status=DiagnosticStatus.PASS if workspace_ok else DiagnosticStatus.FAIL,
            detail="readable and writable" if workspace_ok else "not readable/writable",
        )
    )
    free_gib = shutil.disk_usage(workspace).free / (1024**3)
    checks.append(
        Diagnostic(
            name="Free disk",
            status=DiagnosticStatus.PASS if free_gib >= 1 else DiagnosticStatus.WARN,
            detail=f"{free_gib:.1f} GiB available",
        )
    )
    git_ok, git_detail = probe(["git", "--version"])
    checks.append(
        Diagnostic(
            name="Git",
            status=DiagnosticStatus.PASS if git_ok else DiagnosticStatus.FAIL,
            detail=git_detail,
        )
    )
    missing_model = settings.model.missing_live_fields()
    checks.append(
        Diagnostic(
            name="Model configuration",
            status=DiagnosticStatus.WARN if missing_model else DiagnosticStatus.PASS,
            detail=(
                "missing: " + ", ".join(missing_model)
                if missing_model
                else "complete (no network request made)"
            ),
        )
    )
    if settings.execution.backend in {"auto", "docker"}:
        docker_ok, docker_detail = probe(["docker", "info"])
        required = settings.execution.backend == "docker"
        checks.append(
            Diagnostic(
                name="Docker sandbox",
                status=(
                    DiagnosticStatus.PASS
                    if docker_ok
                    else DiagnosticStatus.FAIL
                    if required
                    else DiagnosticStatus.WARN
                ),
                detail=(
                    docker_detail
                    if docker_ok or required
                    else f"{docker_detail}; review-risk commands will fail closed"
                ),
            )
        )
    else:
        checks.append(
            Diagnostic(
                name="Execution backend",
                status=DiagnosticStatus.WARN,
                detail="local (automatic sandbox routing disabled)",
            )
        )
    return tuple(checks)


def _probe_command(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            creationflags=WINDOWS_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"unavailable ({type(error).__name__})"
    output = (completed.stdout or completed.stderr).strip().splitlines()
    detail = output[0][:200] if output else f"exit code {completed.returncode}"
    return completed.returncode == 0, detail


def has_failures(checks: tuple[Diagnostic, ...]) -> bool:
    """Return whether any required diagnostic failed."""
    return any(check.status is DiagnosticStatus.FAIL for check in checks)
