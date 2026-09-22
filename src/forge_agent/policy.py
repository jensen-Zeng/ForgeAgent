"""Deterministic command risk classification independent of model claims."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RiskLevel(StrEnum):
    """Command execution risk decided by local policy."""

    SAFE = "safe"
    REVIEW = "review"
    DENY = "deny"


class CommandRequest(BaseModel):
    """Already parsed command request; no shell source string exists."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executable: str
    args: tuple[str, ...]
    cwd: str


class CommandReview(BaseModel):
    """Local policy decision presented to an approval handler."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: CommandRequest
    risk: RiskLevel
    reason: str
    execution_backend: Literal["none", "local", "docker"] = "none"
    routing_reason: str = "execution backend not selected"


class CommandPolicy:
    """Allow a narrow toolchain and deny unknown commands by default."""

    _SHELLS = frozenset(
        {
            "bash",
            "cmd",
            "cmd.exe",
            "fish",
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "sh",
            "zsh",
        }
    )
    _NETWORK = frozenset(
        {
            "curl",
            "curl.exe",
            "ftp",
            "gh",
            "scp",
            "ssh",
            "wget",
            "wget.exe",
        }
    )
    _PRIVILEGED = frozenset({"doas", "runas", "sudo"})
    _DESTRUCTIVE = frozenset({"del", "erase", "format", "mkfs", "remove-item", "rm", "rmdir"})
    _PACKAGE_MANAGERS = frozenset({"conda", "npm", "pip", "pip3", "pnpm", "poetry", "uv", "yarn"})
    _SHELL_TOKENS = frozenset({"|", "||", "&&", ">", ">>", "<", "2>", "2>>"})
    _GIT_SAFE = frozenset({"diff", "log", "show", "status"})
    _GIT_DENY = frozenset(
        {
            "checkout",
            "clean",
            "clone",
            "fetch",
            "pull",
            "push",
            "rebase",
            "reset",
            "restore",
        }
    )

    def __init__(self, *, extra_safe_executables: tuple[str, ...] = ()) -> None:
        self._extra_safe = frozenset(name.casefold() for name in extra_safe_executables)

    def classify(self, request: CommandRequest) -> CommandReview:
        """Return the first matching local rule; unknown commands are denied."""
        executable = Path(request.executable).name.casefold()
        arguments = tuple(argument.casefold() for argument in request.args)
        if self._has_shell_syntax(request.args):
            return self._decision(request, RiskLevel.DENY, "shell syntax is not allowed")
        if executable in self._SHELLS:
            return self._decision(request, RiskLevel.DENY, "shell interpreters are denied")
        if executable in self._PRIVILEGED:
            return self._decision(request, RiskLevel.DENY, "privilege escalation is denied")
        if executable in self._DESTRUCTIVE:
            return self._decision(request, RiskLevel.DENY, "destructive commands are denied")
        if executable in self._NETWORK:
            return self._decision(request, RiskLevel.DENY, "network clients are denied")
        if executable in self._extra_safe:
            return self._decision(
                request,
                RiskLevel.SAFE,
                "explicitly allowed by the embedding application",
            )
        if executable in {"pytest", "pytest.exe", "mypy", "mypy.exe"}:
            return self._decision(request, RiskLevel.SAFE, "approved verification tool")
        if executable in {"ruff", "ruff.exe"}:
            if arguments and arguments[0] == "check":
                return self._decision(request, RiskLevel.SAFE, "Ruff check is read-only")
            if arguments and arguments[0] == "format":
                return self._decision(request, RiskLevel.REVIEW, "formatter changes files")
            return self._decision(request, RiskLevel.DENY, "unsupported Ruff operation")
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            return self._classify_python(request, arguments)
        if executable in {"git", "git.exe"}:
            return self._classify_git(request, arguments)
        if executable in {"black", "black.exe"}:
            return self._decision(request, RiskLevel.REVIEW, "formatter changes files")
        if executable in self._PACKAGE_MANAGERS:
            return self._decision(
                request,
                RiskLevel.REVIEW,
                "package operations may change dependencies or access the network",
            )
        return self._decision(request, RiskLevel.DENY, "command is not allowlisted")

    def _classify_python(
        self,
        request: CommandRequest,
        arguments: tuple[str, ...],
    ) -> CommandReview:
        if len(arguments) >= 2 and arguments[0] == "-m":
            module = arguments[1]
            if module in {"pytest", "mypy"}:
                return self._decision(request, RiskLevel.SAFE, f"approved {module} module")
            if module == "ruff" and len(arguments) >= 3:
                if arguments[2] == "check":
                    return self._decision(request, RiskLevel.SAFE, "Ruff check is read-only")
                if arguments[2] == "format":
                    return self._decision(request, RiskLevel.REVIEW, "formatter changes files")
            if module == "pip":
                return self._decision(
                    request,
                    RiskLevel.REVIEW,
                    "pip may change dependencies or access the network",
                )
        return self._decision(request, RiskLevel.DENY, "arbitrary Python execution is denied")

    def _classify_git(
        self,
        request: CommandRequest,
        arguments: tuple[str, ...],
    ) -> CommandReview:
        subcommand = next((arg for arg in arguments if not arg.startswith("-")), "")
        if subcommand in self._GIT_SAFE:
            return self._decision(request, RiskLevel.SAFE, "read-only Git command")
        if subcommand in self._GIT_DENY:
            return self._decision(
                request,
                RiskLevel.DENY,
                "network or working-tree/history-changing Git command",
            )
        return self._decision(request, RiskLevel.DENY, "unsupported Git operation")

    def _has_shell_syntax(self, arguments: tuple[str, ...]) -> bool:
        for argument in arguments:
            if argument in self._SHELL_TOKENS:
                return True
            if any(token in argument for token in ("&&", "||", "`", "$(")):
                return True
        return False

    def _decision(
        self,
        request: CommandRequest,
        risk: RiskLevel,
        reason: str,
    ) -> CommandReview:
        return CommandReview(request=request, risk=risk, reason=reason)
