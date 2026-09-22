"""Tests for the ForgeAgent command-line interface and read-only demo."""

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge_agent import __version__
from forge_agent.cli import app
from forge_agent.domain import ModelResponse, TextPart, ToolCall, ToolCallPart
from forge_agent.models import FakeModelProvider

runner = CliRunner()


def test_help_succeeds() -> None:
    """The CLI exposes its top-level help without requiring configuration."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "transparent, security-conscious coding agent" in result.stdout
    assert "run" in result.stdout
    assert "config" in result.stdout
    assert "tools" in result.stdout
    assert "doctor" in result.stdout
    assert "rlm" in result.stdout
    assert "session" in result.stdout


def test_rlm_refuses_before_model_or_kernel_without_explicit_trust(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["rlm", "run", "task", "--workspace", str(tmp_path)],
        env={},
    )

    assert result.exit_code == 2
    assert "RLM mode refused" in result.stdout
    assert "not sandboxed" in result.stdout
    assert not (tmp_path / ".forge").exists()


def test_session_list_is_read_only_without_model_configuration(tmp_path: Path) -> None:
    result = runner.invoke(app, ["session", "list", "--workspace", str(tmp_path)], env={})

    assert result.exit_code == 0
    assert "ForgeAgent sessions" in result.stdout


def test_version_succeeds() -> None:
    """The CLI reports the package version."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"forge-agent {__version__}"


def test_config_check_can_validate_core_settings_without_credentials(
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "config",
            "check",
            "--workspace",
            str(tmp_path),
            "--allow-missing-model",
        ],
        env={},
    )

    assert result.exit_code == 0
    assert "Core configuration is valid" in result.stdout
    assert "live model settings are incomplete" in result.stdout
    assert "API Key" in result.stdout
    assert "missing" in result.stdout


def test_config_check_displays_selected_docker_backend(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "config",
            "check",
            "--workspace",
            str(tmp_path),
            "--allow-missing-model",
        ],
        env={
            "FORGE_EXECUTION_BACKEND": "docker",
            "FORGE_DOCKER_IMAGE": "forge-agent:test",
        },
    )

    assert result.exit_code == 0
    assert "docker" in result.stdout
    assert "forge-agent:test" in result.stdout


def test_doctor_succeeds_with_warnings_and_never_needs_api_key(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["doctor", "--workspace", str(tmp_path)],
        env={},
    )

    assert result.exit_code == 0
    assert "ForgeAgent doctor" in result.stdout
    assert "Model configuration" in result.stdout
    assert "warn" in result.stdout


def test_config_check_missing_model_is_actionable(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["config", "check", "--workspace", str(tmp_path)],
        env={},
    )

    assert result.exit_code == 2
    assert "Configuration error" in result.stdout
    assert "FORGE_MODEL_API_KEY" in result.stdout
    assert "MODEL_CONFIGURATION.md" in result.stdout


def test_config_check_redacts_api_key_and_does_not_make_request(tmp_path: Path) -> None:
    secret = "sk-test-secret-value"
    result = runner.invoke(
        app,
        ["config", "check", "--workspace", str(tmp_path)],
        env={
            "FORGE_MODEL_PROVIDER": "openai",
            "FORGE_MODEL_BASE_URL": "https://api.example.com/v1",
            "FORGE_MODEL_NAME": "test-model",
            "FORGE_MODEL_API_KEY": secret,
        },
    )

    assert result.exit_code == 0
    assert "configured (redacted)" in result.stdout
    assert "No network request was made" in result.stdout
    assert secret not in result.stdout


def test_tools_list_is_sorted_and_needs_no_model_credentials(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["tools", "list", "--workspace", str(tmp_path)],
        env={},
    )

    assert result.exit_code == 0
    assert result.stdout.index("list_files") < result.stdout.index("read_file")
    assert result.stdout.index("read_file") < result.stdout.index("search_code")
    assert tmp_path.name in result.stdout.replace("\n", "")


def scripted_read_provider() -> FakeModelProvider:
    """Build the deterministic provider used by CLI end-to-end tests."""
    return FakeModelProvider(
        (
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="call-list",
                            name="list_files",
                            arguments={"path": ".", "max_depth": 2},
                        )
                    ),
                )
            ),
            ModelResponse(
                content=(
                    ToolCallPart(
                        tool_call=ToolCall(
                            id="call-read",
                            name="read_file",
                            arguments={"path": "app.py"},
                        )
                    ),
                )
            ),
            ModelResponse(
                content=(TextPart(text="入口文件为 app.py。main() 调用 format_note 后打印便签。"),)
            ),
        )
    )


def test_run_fake_provider_completes_and_writes_replayable_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "app.py").write_text(
        "def main():\n    print('hello')\n",
        encoding="utf-8",
    )
    trace_path = tmp_path / "run.jsonl"
    provider = scripted_read_provider()
    monkeypatch.setattr(
        "forge_agent.cli._create_live_provider",
        lambda settings: provider,
    )

    result = runner.invoke(
        app,
        [
            "run",
            "解释入口",
            "--workspace",
            str(tmp_path),
            "--trace",
            str(trace_path),
        ],
        env={},
    )

    assert result.exit_code == 0
    assert "Step 1: calling model" in result.stdout
    assert "Calling tool list_files" in result.stdout
    assert "Calling tool read_file" in result.stdout
    assert "入口文件为 app.py" in result.stdout
    assert "completed" in result.stdout
    assert provider.closed is True
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [record["sequence"] for record in records] == list(range(1, len(records) + 1))
    requested = [record for record in records if record["kind"] == "tool_call_requested"]
    assert requested[1]["data"]["arguments"]["path"] == "app.py"
    completed = [record for record in records if record["kind"] == "tool_call_completed"]
    assert "def main" in completed[1]["data"]["output"]
    assert records[-1]["kind"] == "run_completed"
    assert "入口文件为 app.py" in records[-1]["data"]["final_text"]


def test_run_without_live_configuration_fails_before_writing_trace(
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "missing-config.jsonl"

    result = runner.invoke(
        app,
        [
            "run",
            "Inspect",
            "--workspace",
            str(tmp_path),
            "--trace",
            str(trace_path),
        ],
        env={},
    )

    assert result.exit_code == 2
    assert "FORGE_MODEL_API_KEY" in result.stdout
    assert trace_path.exists() is False


def test_cancelled_fake_run_uses_exit_130_and_closes_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace_path = tmp_path / "cancelled.jsonl"
    provider = FakeModelProvider((asyncio.CancelledError(),))
    monkeypatch.setattr(
        "forge_agent.cli._create_live_provider",
        lambda settings: provider,
    )

    result = runner.invoke(
        app,
        [
            "run",
            "Cancel",
            "--workspace",
            str(tmp_path),
            "--trace",
            str(trace_path),
        ],
        env={},
    )

    assert result.exit_code == 130
    assert "cancelled" in result.stdout.lower()
    assert provider.closed is True
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["kind"] == "run_cancelled"
