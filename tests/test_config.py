"""Tests for layered and secret-safe ForgeAgent configuration."""

from decimal import Decimal
from pathlib import Path

import pytest

from forge_agent.config import ConfigurationError, load_settings


def write_config(path: Path, content: str) -> None:
    """Write a TOML fixture using explicit UTF-8 encoding."""
    path.write_text(content, encoding="utf-8")


def test_defaults_are_valid_without_live_credentials(tmp_path: Path) -> None:
    settings = load_settings(cwd=tmp_path, environ={})

    assert settings.workspace.allowed_root == tmp_path.resolve()
    assert settings.agent.max_steps == 30
    assert settings.agent.max_repeated_failures == 3
    assert settings.commands.approval == "risky"
    assert settings.execution.backend == "auto"
    assert settings.model.missing_live_fields() == (
        "FORGE_MODEL_API_KEY",
        "FORGE_MODEL_BASE_URL",
        "FORGE_MODEL_NAME",
    )


def test_missing_live_configuration_is_explicit(tmp_path: Path) -> None:
    settings = load_settings(cwd=tmp_path, environ={})

    with pytest.raises(ConfigurationError, match="FORGE_MODEL_API_KEY"):
        settings.model.require_live_configuration()


def test_priority_is_cli_then_environment_then_file_then_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "forge.toml"
    write_config(
        config_file,
        """
[model]
name = "file-model"

[agent]
max_steps = 10
timeout_seconds = 100

[commands]
approval = "always"
""".strip(),
    )
    environ = {
        "FORGE_MODEL_NAME": "environment-model",
        "FORGE_AGENT_MAX_STEPS": "20",
    }
    cli = {
        "model": {"name": "cli-model"},
        "agent": {"max_steps": 25},
    }

    settings = load_settings(
        config_path=config_file,
        cli_overrides=cli,
        environ=environ,
        cwd=tmp_path,
    )

    assert settings.model.name == "cli-model"
    assert settings.agent.max_steps == 25
    assert settings.agent.timeout_seconds == Decimal("100")
    assert settings.commands.approval == "always"


def test_environment_values_are_parsed(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={
            "FORGE_MODEL_API_KEY": "secret",
            "FORGE_MODEL_BASE_URL": "https://api.example.com/v1",
            "FORGE_MODEL_NAME": "coding-model",
            "FORGE_WORKSPACE_RESPECT_GITIGNORE": "false",
            "FORGE_AGENT_MAX_COST_USD": "2.50",
            "FORGE_AGENT_MAX_REPEATED_FAILURES": "4",
        },
    )

    settings.model.require_live_configuration()
    assert settings.workspace.respect_gitignore is False
    assert settings.agent.max_cost_usd == Decimal("2.50")
    assert settings.agent.max_repeated_failures == 4


def test_relative_workspace_is_resolved_from_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    config_file = tmp_path / "forge.toml"
    write_config(config_file, '[workspace]\nallowed_root = "repo"\n')

    settings = load_settings(config_path=config_file, cwd=tmp_path, environ={})

    assert settings.workspace.allowed_root == workspace.resolve()
    assert settings.workspace.allowed_root.is_absolute()


def test_windows_absolute_workspace_is_supported(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={"FORGE_WORKSPACE_ALLOWED_ROOT": str(tmp_path)},
    )

    assert settings.workspace.allowed_root == tmp_path.resolve()
    assert settings.workspace.allowed_root.drive == tmp_path.resolve().drive


def test_missing_workspace_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(ConfigurationError, match="Workspace does not exist"):
        load_settings(
            cwd=tmp_path,
            environ={"FORGE_WORKSPACE_ALLOWED_ROOT": str(missing)},
        )


def test_workspace_file_is_rejected(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-directory.txt"
    file_path.write_text("content", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="not a directory"):
        load_settings(
            cwd=tmp_path,
            environ={"FORGE_WORKSPACE_ALLOWED_ROOT": str(file_path)},
        )


def test_invalid_url_is_rejected_without_leaking_secret(tmp_path: Path) -> None:
    secret = "super-secret-value"

    with pytest.raises(ConfigurationError) as captured:
        load_settings(
            cwd=tmp_path,
            environ={
                "FORGE_MODEL_API_KEY": secret,
                "FORGE_MODEL_BASE_URL": "not-a-url",
            },
        )

    assert secret not in str(captured.value)


def test_non_positive_budget_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="greater than 0"):
        load_settings(
            cwd=tmp_path,
            environ={"FORGE_AGENT_MAX_STEPS": "-1"},
        )


def test_blank_model_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="model name cannot be blank"):
        load_settings(
            cwd=tmp_path,
            environ={"FORGE_MODEL_NAME": "   "},
        )


def test_public_serialization_never_contains_api_key(tmp_path: Path) -> None:
    secret = "do-not-print-this-key"
    settings = load_settings(
        cwd=tmp_path,
        environ={
            "FORGE_MODEL_API_KEY": secret,
            "FORGE_MODEL_BASE_URL": "https://api.example.com/v1",
            "FORGE_MODEL_NAME": "coding-model",
        },
    )

    serialized = str(settings.public_dict())

    assert secret not in serialized
    assert "<redacted>" in serialized


def test_explicit_missing_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_settings(
            config_path=tmp_path / "missing.toml",
            cwd=tmp_path,
            environ={},
        )


def test_unknown_config_key_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "forge.toml"
    write_config(config_file, "[agent]\nunknown_option = true\n")

    with pytest.raises(ConfigurationError, match="extra_forbidden"):
        load_settings(config_path=config_file, cwd=tmp_path, environ={})


def test_run_config_uses_validated_settings(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={"FORGE_MODEL_NAME": "coding-model"},
    )

    run_config = settings.to_run_config("Fix the failing test")

    assert run_config.task == "Fix the failing test"
    assert run_config.workspace == tmp_path.resolve()
    assert run_config.model_name == "coding-model"
    assert run_config.budget.max_steps == settings.agent.max_steps


def test_docker_execution_settings_are_loaded_from_environment(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={
            "FORGE_EXECUTION_BACKEND": "docker",
            "FORGE_DOCKER_IMAGE": "forge-agent:test",
            "FORGE_DOCKER_CPUS": "2",
            "FORGE_DOCKER_MEMORY": "768m",
            "FORGE_DOCKER_PIDS_LIMIT": "64",
        },
    )

    assert settings.execution.backend == "docker"
    assert settings.execution.docker_image == "forge-agent:test"
    assert settings.execution.docker_cpus == 2
    assert settings.execution.docker_memory == "768m"
    assert settings.execution.docker_pids_limit == 64


def test_automatic_execution_backend_can_be_selected_explicitly(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={"FORGE_EXECUTION_BACKEND": "auto"},
    )

    assert settings.execution.backend == "auto"


def test_model_pricing_requires_input_and_output_pair(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="must be set together"):
        load_settings(
            cwd=tmp_path,
            environ={"FORGE_MODEL_INPUT_COST_PER_MILLION": "2.50"},
        )


def test_model_pricing_snapshot_is_parsed_as_decimal(tmp_path: Path) -> None:
    settings = load_settings(
        cwd=tmp_path,
        environ={
            "FORGE_MODEL_INPUT_COST_PER_MILLION": "2.50",
            "FORGE_MODEL_OUTPUT_COST_PER_MILLION": "10",
            "FORGE_MODEL_CACHED_INPUT_COST_PER_MILLION": "1.25",
        },
    )

    assert settings.model.input_cost_per_million == Decimal("2.50")
    assert settings.model.output_cost_per_million == Decimal("10")
    assert settings.model.cached_input_cost_per_million == Decimal("1.25")


def test_patch_approval_defaults_safe_and_can_be_disabled(tmp_path: Path) -> None:
    default = load_settings(cwd=tmp_path, environ={})
    disabled = load_settings(
        cwd=tmp_path,
        environ={"FORGE_PATCHES_APPROVAL": "never"},
    )

    assert default.patches.approval == "always"
    assert disabled.patches.approval == "never"
