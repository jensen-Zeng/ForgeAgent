"""CLI isolation keeps agent output separate from a human's checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge_agent.cli import _build_registry, app
from forge_agent.config import load_settings
from forge_agent.domain import ModelResponse, TextPart, ToolCall, ToolCallPart
from forge_agent.locking import acquire_locks
from forge_agent.models import FakeModelProvider
from forge_agent.session import SessionStore
from forge_agent.worktrees import WorktreeManager

runner = CliRunner()


def _repo(path: Path) -> None:
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


def _patch_provider() -> FakeModelProvider:
    return FakeModelProvider(
        (
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="read",
                            name="read_file",
                            arguments={"path": "app.py"},
                        )
                    ),
                )
            ),
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="edit",
                            name="apply_patch",
                            arguments={
                                "edits": [
                                    {
                                        "path": "app.py",
                                        "old_text": "VALUE = 1",
                                        "new_text": "VALUE = 2",
                                    }
                                ]
                            },
                        )
                    ),
                )
            ),
            ModelResponse(content=(TextPart(text="Done"),)),
        )
    )


def test_default_run_keeps_changes_in_isolated_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo(tmp_path)
    provider = _patch_provider()
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    result = runner.invoke(
        app, ["run", "change value", "-w", str(tmp_path)], env={"FORGE_PATCHES_APPROVAL": "never"}
    )
    assert result.exit_code == 0, result.output
    assert provider.closed
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    (isolated,) = WorktreeManager(tmp_path).list()
    assert (isolated.path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert "Isolated workspace" in result.output
    review = runner.invoke(app, ["workspace", "diff", isolated.id, "-w", str(tmp_path)])
    assert review.exit_code == 0
    assert "+VALUE = 2" in review.output


def test_non_git_default_refuses_without_silent_in_place_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _patch_provider()
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    result = runner.invoke(app, ["run", "task", "-w", str(tmp_path)])
    assert result.exit_code == 2
    assert "Workspace error" in result.output
    assert provider.closed
    assert not (tmp_path / "app.py").exists()


def test_second_in_place_cli_rejected_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _patch_provider()
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    with acquire_locks((tmp_path / ".forge" / "active-run",)):
        result = runner.invoke(app, ["run", "task", "--in-place", "-w", str(tmp_path)])
    assert result.exit_code == 2
    assert "workspace_busy" in result.output
    assert provider.closed


def test_production_registry_requires_host_observed_read(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("old", encoding="utf-8")
    settings = load_settings(cwd=tmp_path, environ={"FORGE_PATCHES_APPROVAL": "never"})
    registry = _build_registry(settings)
    result = registry.execute(
        ToolCall(
            id="edit",
            name="apply_patch",
            arguments={
                "edits": [
                    {"path": "app.py", "old_text": "old", "new_text": "new"},
                ]
            },
        )
    )
    assert result.metadata["error_code"] == "read_required"


def test_setup_failure_closes_provider_and_releases_workspace_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _patch_provider()
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)

    def fail_registry(*args: object, **kwargs: object) -> None:
        raise RuntimeError("registry initialization failed")

    monkeypatch.setattr("forge_agent.cli._build_registry", fail_registry)
    result = runner.invoke(app, ["run", "task", "--in-place", "-w", str(tmp_path)])
    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert provider.closed
    with acquire_locks((tmp_path / ".forge" / "active-run",), timeout=0):
        pass


def test_nested_in_place_workspace_uses_checkout_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo(tmp_path)
    nested = tmp_path / "src"
    nested.mkdir()
    provider = _patch_provider()
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    with acquire_locks((tmp_path / ".forge" / "active-run",)):
        result = runner.invoke(app, ["run", "task", "--in-place", "-w", str(nested)])
    assert result.exit_code == 2
    assert "workspace_busy" in result.output
    assert provider.closed


def test_default_rlm_session_is_stored_and_resumed_in_its_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo(tmp_path)
    provider = FakeModelProvider((ModelResponse(content=(TextPart(text="Done"),)),))
    monkeypatch.setattr("forge_agent.cli._create_live_provider", lambda settings: provider)
    result = runner.invoke(app, ["rlm", "run", "task", "--trust-workspace", "-w", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert provider.closed
    (isolated,) = WorktreeManager(tmp_path).list()
    (directory,) = (isolated.path / ".forge" / "sessions").iterdir()
    store = SessionStore.open(isolated.path, directory.name)
    assert Path(store.header.workspace) == isolated.path
    assert not (tmp_path / ".forge" / "sessions").exists()
    provider = FakeModelProvider(())
    resumed = runner.invoke(
        app,
        ["rlm", "resume", directory.name, "--trust-workspace", "-w", str(isolated.path)],
    )
    assert resumed.exit_code == 0, resumed.output
    assert "No unconsumed input" in resumed.output
    assert provider.closed


def test_session_repair_refuses_while_runtime_owns_journal(tmp_path: Path) -> None:
    store = SessionStore.create(tmp_path)
    with acquire_locks((store.directory / "runtime-owner",)):
        result = runner.invoke(
            app,
            [
                "session",
                "repair",
                str(store.header.session_id),
                "-w",
                str(tmp_path),
                "--truncate-tail",
            ],
        )
    assert result.exit_code == 2
    assert "Unable to repair session" in result.output
