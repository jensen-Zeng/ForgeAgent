"""Tests for the hardened Docker sandbox used by automatic risk routing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge_agent.config import load_settings
from forge_agent.sandbox import (
    DockerSandbox,
    DockerSandboxConfig,
    SandboxUnavailableError,
    configured_docker_sandbox,
)


def test_command_has_only_bounded_workspace_mount(tmp_path: Path) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )

    command = sandbox.build_command(("forge", "run", "inspect"))
    joined = " ".join(command)

    assert "--network none" in joined
    assert "--read-only" in command
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--user 10001:10001" in joined
    assert "--memory 1g" in joined
    assert "--cpus 1.0" in joined
    assert "--pids-limit 128" in joined
    assert "--entrypoint forge" in joined
    assert joined.endswith("forge-agent:dev run inspect")
    assert command.count("--mount") == 1
    assert f"source={tmp_path.resolve()},target=/workspace" in joined
    assert "--privileged" not in command
    assert "docker.sock" not in joined


def test_missing_docker_is_reported_explicitly(tmp_path: Path) -> None:
    sandbox = DockerSandbox(DockerSandboxConfig(workspace=tmp_path))
    sandbox._docker = None

    with pytest.raises(SandboxUnavailableError, match="not installed"):
        sandbox.run(("forge", "--help"))


def test_run_does_not_invoke_a_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )

    result = sandbox.run(("forge", "--help"))

    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert observed["shell"] is False


def test_timeout_is_a_structured_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def time_out(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        if command[1:3] == ["rm", "--force"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise subprocess.TimeoutExpired("docker", 1, output="partial", stderr="late")

    monkeypatch.setattr(subprocess, "run", time_out)
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path, timeout_seconds=1),
        docker_executable="docker",
    )

    result = sandbox.run(("forge", "--help"), timeout_seconds=0.5)

    assert result.timed_out is True
    assert result.exit_code == -1
    assert result.stdout == "partial"
    assert calls[0][0:2] == ["docker", "run"]
    assert calls[1][0:3] == ["docker", "rm", "--force"]
    assert calls[1][-1].startswith("forge-agent-")


def test_empty_target_command_is_rejected(tmp_path: Path) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )

    with pytest.raises(ValueError, match="cannot be empty"):
        sandbox.build_command(())


def test_container_working_directory_stays_under_workspace(tmp_path: Path) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(workspace=tmp_path),
        docker_executable="docker",
    )

    command = sandbox.build_command(("pytest", "-q"), working_directory="tests/unit")

    assert "--workdir /workspace/tests/unit" in " ".join(command)
    with pytest.raises(ValueError, match="stay inside"):
        sandbox.build_command(("pytest",), working_directory="../outside")


def test_mount_path_with_comma_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "has,comma"
    workspace.mkdir()

    with pytest.raises(ValidationError, match="cannot contain a comma"):
        DockerSandboxConfig(workspace=workspace)


def test_checked_in_container_specs_keep_security_invariants() -> None:
    root = Path(__file__).parents[1]
    dockerfile = (root / "docker" / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker" / "compose.yaml").read_text(encoding="utf-8")

    assert "USER 10001:10001" in dockerfile
    assert "network_mode: none" in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "no-new-privileges:true" in compose
    assert "pids_limit:" in compose
    assert "mem_limit:" in compose
    assert "source: ${FORGE_WORKSPACE:?set FORGE_WORKSPACE}" in compose
    assert "docker.sock" not in compose
    assert "privileged:" not in compose


def test_target_dependency_image_installs_only_from_offline_wheelhouse() -> None:
    root = Path(__file__).parents[1]
    target = (root / "docker" / "target.Dockerfile.example").read_text(encoding="utf-8")

    assert "--no-index" in target
    assert "--find-links=/tmp/target/wheels" in target
    assert "requirements.lock" in target
    assert target.rstrip().endswith("USER 10001:10001")


def test_configured_sandbox_is_shared_by_auto_and_docker_modes(tmp_path: Path) -> None:
    automatic = load_settings(cwd=tmp_path, environ={})
    forced = load_settings(
        cwd=tmp_path,
        environ={"FORGE_EXECUTION_BACKEND": "docker"},
    )
    local = load_settings(
        cwd=tmp_path,
        environ={"FORGE_EXECUTION_BACKEND": "local"},
    )

    auto_sandbox = configured_docker_sandbox(automatic)
    docker_sandbox = configured_docker_sandbox(forced)

    assert auto_sandbox is not None
    assert auto_sandbox.config.workspace == tmp_path.resolve()
    assert docker_sandbox is not None
    assert configured_docker_sandbox(local) is None
