"""Layered, validated, and secret-safe ForgeAgent configuration."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from forge_agent.domain import Budget, RunConfig

ConfigData = dict[str, Any]

ENV_PATHS: Final[dict[str, tuple[str, str]]] = {
    "FORGE_MODEL_PROVIDER": ("model", "provider"),
    "FORGE_MODEL_API_KEY": ("model", "api_key"),
    "FORGE_MODEL_BASE_URL": ("model", "base_url"),
    "FORGE_MODEL_NAME": ("model", "name"),
    "FORGE_MODEL_TIMEOUT_SECONDS": ("model", "total_timeout_seconds"),
    "FORGE_MODEL_CONNECT_TIMEOUT_SECONDS": ("model", "connect_timeout_seconds"),
    "FORGE_MODEL_READ_TIMEOUT_SECONDS": ("model", "read_timeout_seconds"),
    "FORGE_MODEL_TOTAL_TIMEOUT_SECONDS": ("model", "total_timeout_seconds"),
    "FORGE_MODEL_MAX_OUTPUT_TOKENS": ("model", "max_output_tokens"),
    "FORGE_MODEL_MAX_RETRIES": ("model", "max_retries"),
    "FORGE_MODEL_INPUT_COST_PER_MILLION": ("model", "input_cost_per_million"),
    "FORGE_MODEL_OUTPUT_COST_PER_MILLION": ("model", "output_cost_per_million"),
    "FORGE_MODEL_CACHED_INPUT_COST_PER_MILLION": (
        "model",
        "cached_input_cost_per_million",
    ),
    "FORGE_ANTHROPIC_VERSION": ("model", "anthropic_version"),
    "FORGE_AGENT_MAX_STEPS": ("agent", "max_steps"),
    "FORGE_AGENT_TIMEOUT_SECONDS": ("agent", "timeout_seconds"),
    "FORGE_AGENT_MAX_TOOL_CALLS": ("agent", "max_tool_calls"),
    "FORGE_AGENT_MAX_REPEATED_FAILURES": ("agent", "max_repeated_failures"),
    "FORGE_AGENT_MAX_TOKENS": ("agent", "max_tokens"),
    "FORGE_AGENT_MAX_COST_USD": ("agent", "max_cost_usd"),
    "FORGE_WORKSPACE_ALLOWED_ROOT": ("workspace", "allowed_root"),
    "FORGE_WORKSPACE_RESPECT_GITIGNORE": ("workspace", "respect_gitignore"),
    "FORGE_COMMANDS_APPROVAL": ("commands", "approval"),
    "FORGE_PATCHES_APPROVAL": ("patches", "approval"),
    "FORGE_EXECUTION_BACKEND": ("execution", "backend"),
    "FORGE_DOCKER_IMAGE": ("execution", "docker_image"),
    "FORGE_DOCKER_CPUS": ("execution", "docker_cpus"),
    "FORGE_DOCKER_MEMORY": ("execution", "docker_memory"),
    "FORGE_DOCKER_PIDS_LIMIT": ("execution", "docker_pids_limit"),
    "FORGE_CONTEXT_MAX_TOKENS": ("context", "max_tokens"),
    "FORGE_CONTEXT_WINDOW_TOKENS": ("context", "window_tokens"),
    "FORGE_CONTEXT_RESERVE_TOKENS": ("context", "reserve_tokens"),
    "FORGE_CONTEXT_KEEP_RECENT_TOKENS": ("context", "keep_recent_tokens"),
    "FORGE_CONTEXT_MAX_TOOL_RESULT_CHARS": ("context", "max_tool_result_chars"),
    "FORGE_COMPACTION_ENABLED": ("context", "compaction_enabled"),
    "FORGE_CONTEXT_COMPRESSION_RATIO": ("context", "compression_ratio"),
    "FORGE_CONTEXT_REPEATED_CALL_LIMIT": ("context", "repeated_call_limit"),
    "FORGE_RLM_MAX_DEPTH": ("rlm", "max_depth"),
    "FORGE_RLM_MAX_CONCURRENT_CHILDREN": ("rlm", "max_concurrent_children"),
    "FORGE_RLM_CHILD_OVERFLOW": ("rlm", "child_overflow"),
    "FORGE_RLM_HOST_REQUEST_TIMEOUT_SECONDS": ("rlm", "host_request_timeout_seconds"),
    "FORGE_SESSION_INPUT_LIMIT": ("session", "max_input_chars"),
}


class ConfigurationError(ValueError):
    """Raised when configuration cannot be loaded or used safely."""


class SettingsModel(BaseModel):
    """Strict immutable base for application settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelSettings(SettingsModel):
    """Configuration shared by supported live model providers."""

    provider: Literal["openai", "anthropic", "ollama"] = "openai"
    api_key: SecretStr | None = None
    base_url: AnyHttpUrl | None = None
    name: str | None = None
    connect_timeout_seconds: Decimal = Field(default=Decimal("10"), gt=0)
    read_timeout_seconds: Decimal = Field(default=Decimal("60"), gt=0)
    total_timeout_seconds: Decimal = Field(default=Decimal("120"), gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    anthropic_version: str = Field(default="2023-06-01", min_length=1)
    input_cost_per_million: Decimal | None = Field(default=None, ge=0)
    output_cost_per_million: Decimal | None = Field(default=None, ge=0)
    cached_input_cost_per_million: Decimal | None = Field(default=None, ge=0)

    @field_validator("name")
    @classmethod
    def reject_blank_name(cls, value: str | None) -> str | None:
        """Allow an omitted model but reject an ambiguous blank name."""
        if value is not None and not value.strip():
            raise ValueError("model name cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_pricing_pair(self) -> ModelSettings:
        """Require enough information to calculate a complete response cost."""
        has_input = self.input_cost_per_million is not None
        has_output = self.output_cost_per_million is not None
        if has_input != has_output:
            raise ValueError(
                "input_cost_per_million and output_cost_per_million must be set together"
            )
        if self.cached_input_cost_per_million is not None and not has_input:
            raise ValueError("cached_input_cost_per_million requires input and output pricing")
        return self

    def missing_live_fields(self) -> tuple[str, ...]:
        """Return environment variable names required for a live request."""
        missing: list[str] = []
        if self.provider != "ollama" and (
            self.api_key is None or not self.api_key.get_secret_value().strip()
        ):
            missing.append("FORGE_MODEL_API_KEY")
        if self.base_url is None:
            missing.append("FORGE_MODEL_BASE_URL")
        if self.name is None or not self.name.strip():
            missing.append("FORGE_MODEL_NAME")
        return tuple(missing)

    def require_live_configuration(self) -> None:
        """Fail explicitly when live-provider configuration is incomplete."""
        missing = self.missing_live_fields()
        if missing:
            joined = ", ".join(missing)
            raise ConfigurationError(f"Missing live model configuration: {joined}")


class AgentSettings(SettingsModel):
    """Runtime budgets shared by agent runs."""

    max_steps: int = Field(default=30, gt=0)
    timeout_seconds: Decimal = Field(default=Decimal("900"), gt=0)
    max_tool_calls: int = Field(default=60, gt=0)
    max_repeated_failures: int = Field(default=3, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: Decimal | None = Field(default=Decimal("1.0"), gt=0)

    def to_budget(self) -> Budget:
        """Create an immutable domain budget."""
        return Budget(
            max_steps=self.max_steps,
            timeout_seconds=self.timeout_seconds,
            max_tool_calls=self.max_tool_calls,
            max_repeated_failures=self.max_repeated_failures,
            max_tokens=self.max_tokens,
            max_cost=self.max_cost_usd,
        )


class WorkspaceSettings(SettingsModel):
    """Filesystem boundary applied to agent operations."""

    allowed_root: Path
    respect_gitignore: bool = True


class CommandSettings(SettingsModel):
    """User-approval behavior for future command execution."""

    approval: Literal["always", "risky", "never"] = "risky"


class ExecutionSettings(SettingsModel):
    """Select local, Docker-only, or risk-aware automatic command execution."""

    backend: Literal["local", "docker", "auto"] = "auto"
    docker_image: str = Field(default="forge-agent:dev", min_length=1)
    docker_cpus: float = Field(default=1.0, gt=0, le=8)
    docker_memory: str = Field(default="1g", pattern=r"^[1-9][0-9]*(?:m|g)$")
    docker_pids_limit: int = Field(default=128, gt=0, le=1024)


class PatchSettings(SettingsModel):
    """Human confirmation behavior for file modifications."""

    approval: Literal["always", "never"] = "always"


class ContextSettings(SettingsModel):
    """Heuristic context and no-progress limits."""

    max_tokens: int = Field(default=32_000, gt=0)
    window_tokens: int | None = Field(default=None, gt=0)
    reserve_tokens: int = Field(default=8_192, gt=0)
    keep_recent_tokens: int = Field(default=12_000, gt=0)
    max_tool_result_chars: int = Field(default=2_000, gt=0)
    compaction_enabled: bool = True
    compression_ratio: Decimal = Field(default=Decimal("0.75"), gt=0, le=1)
    repeated_call_limit: int = Field(default=3, gt=1)
    preserve_recent_messages: int = Field(default=4, ge=2, le=20)

    @model_validator(mode="after")
    def normalize_window(self) -> ContextSettings:
        """Keep FORGE_CONTEXT_MAX_TOKENS as the compatibility source."""
        if self.window_tokens is None:
            object.__setattr__(self, "window_tokens", self.max_tokens)
        if self.reserve_tokens >= (self.window_tokens or self.max_tokens):
            raise ValueError("reserve_tokens must be lower than window_tokens")
        return self


class RLMSettings(SettingsModel):
    """Trusted recursive language-model runtime limits."""

    max_depth: int = Field(default=1, ge=0, le=8)
    max_concurrent_children: int = Field(default=4, gt=0, le=32)
    child_overflow: Literal["reject", "queue"] = "reject"
    host_request_timeout_seconds: Decimal = Field(default=Decimal("10"), gt=0, le=60)
    kernel_preview_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)
    kernel_interrupt_timeout_seconds: Decimal = Field(default=Decimal("10"), gt=0, le=60)


class SessionSettings(SettingsModel):
    """Durable session and mailbox bounds."""

    max_input_chars: int = Field(default=1_000_000, gt=0)
    max_message_chars: int = Field(default=32_000, gt=0)
    messages_per_minute: int = Field(default=60, gt=0)
    max_pending_messages: int = Field(default=100, gt=0)
    max_family_messages: int = Field(default=1_000, gt=0)


class ForgeSettings(SettingsModel):
    """Complete validated application settings."""

    model: ModelSettings = Field(default_factory=ModelSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    workspace: WorkspaceSettings
    commands: CommandSettings = Field(default_factory=CommandSettings)
    patches: PatchSettings = Field(default_factory=PatchSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    rlm: RLMSettings = Field(default_factory=RLMSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)

    def public_dict(self) -> dict[str, Any]:
        """Serialize settings without exposing model credentials."""
        dumped = self.model_dump(mode="json")
        dumped["model"]["api_key"] = "<redacted>" if self.model.api_key else None
        return dumped

    def to_run_config(self, task: str) -> RunConfig:
        """Create the domain snapshot consumed by a future runtime."""
        return RunConfig(
            task=task,
            workspace=self.workspace.allowed_root,
            model_name=self.model.name,
            budget=self.agent.to_budget(),
        )


def load_settings(
    *,
    config_path: Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> ForgeSettings:
    """Load defaults, TOML, environment, and CLI values in ascending priority."""
    base_dir = (cwd or Path.cwd()).expanduser().resolve(strict=False)
    resolved_config = _resolve_config_path(config_path, base_dir)
    from_file = _load_toml(resolved_config, required=config_path is not None)
    from_env = _read_environment(environ or os.environ)

    merged: ConfigData = {}
    for source in (from_file, from_env, dict(cli_overrides or {})):
        merged = _deep_merge(merged, source)

    _normalize_workspace(merged, base_dir)
    try:
        return ForgeSettings.model_validate(merged)
    except ValidationError as error:
        message = _redact_validation_error(error, merged)
        raise ConfigurationError(f"Invalid ForgeAgent configuration: {message}") from None


def _resolve_config_path(config_path: Path | None, base_dir: Path) -> Path:
    candidate = config_path or Path("forge.toml")
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve(strict=False)


def _load_toml(path: Path, *, required: bool) -> ConfigData:
    if not path.exists():
        if required:
            raise ConfigurationError(f"Configuration file does not exist: {path}")
        return {}
    if not path.is_file():
        raise ConfigurationError(f"Configuration path is not a file: {path}")
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Unable to read configuration file {path}: {error}") from None
    return dict(data)


def _read_environment(environ: Mapping[str, str]) -> ConfigData:
    data: ConfigData = {}
    for variable, path in ENV_PATHS.items():
        if variable not in environ:
            continue
        section, field = path
        section_data = data.setdefault(section, {})
        section_data[field] = environ[variable]
    return data


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> ConfigData:
    result = deepcopy(dict(base))
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = deepcopy(value)
    return result


def _normalize_workspace(data: ConfigData, base_dir: Path) -> None:
    workspace = data.setdefault("workspace", {})
    if not isinstance(workspace, dict):
        return
    raw_root = workspace.get("allowed_root", base_dir)
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        root = base_dir / root
    root = root.resolve(strict=False)
    if not root.exists():
        raise ConfigurationError(f"Workspace does not exist: {root}")
    if not root.is_dir():
        raise ConfigurationError(f"Workspace is not a directory: {root}")
    workspace["allowed_root"] = root


def _redact_validation_error(error: ValidationError, data: Mapping[str, Any]) -> str:
    message = str(error)
    model_data = data.get("model")
    if isinstance(model_data, Mapping):
        secret = model_data.get("api_key")
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "<redacted>")
    return message
