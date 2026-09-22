"""Tests for read-only environment diagnostics."""

from pathlib import Path

from forge_agent.config import load_settings
from forge_agent.doctor import DiagnosticStatus, has_failures, run_diagnostics


def test_auto_diagnostics_warn_when_optional_docker_is_unavailable(tmp_path: Path) -> None:
    settings = load_settings(cwd=tmp_path, environ={})

    checks = run_diagnostics(
        settings,
        command_probe=lambda command: (
            (False, "daemon unavailable")
            if command == ["docker", "info"]
            else (True, "git version test")
        ),
    )
    statuses = {check.name: check.status for check in checks}

    assert statuses["Git"] is DiagnosticStatus.PASS
    assert statuses["Model configuration"] is DiagnosticStatus.WARN
    assert statuses["Docker sandbox"] is DiagnosticStatus.WARN
    assert has_failures(checks) is False


def test_selected_docker_backend_is_required(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={"FORGE_EXECUTION_BACKEND": "docker"},
    )

    checks = run_diagnostics(
        settings,
        command_probe=lambda command: (
            (False, "daemon unavailable")
            if command == ["docker", "info"]
            else (True, "git version test")
        ),
    )

    docker = next(check for check in checks if check.name == "Docker sandbox")
    assert docker.status is DiagnosticStatus.FAIL
    assert has_failures(checks) is True


def test_explicit_local_backend_warns_that_auto_routing_is_disabled(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={"FORGE_EXECUTION_BACKEND": "local"},
    )

    checks = run_diagnostics(
        settings,
        command_probe=lambda command: (True, "git version test"),
    )

    local = next(check for check in checks if check.name == "Execution backend")
    assert local.status is DiagnosticStatus.WARN
    assert "disabled" in local.detail
