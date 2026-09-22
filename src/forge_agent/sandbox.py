"""Docker sandbox specification and bounded execution."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge_agent.processes import WINDOWS_CREATE_NO_WINDOW

if TYPE_CHECKING:
    from forge_agent.config import ForgeSettings


class SandboxError(RuntimeError):
    """Base error for unavailable or failed container execution."""


class SandboxUnavailableError(SandboxError):
    """Docker executable or daemon is unavailable."""


class DockerSandboxConfig(BaseModel):
    """Non-privileged, no-network container limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace: Path
    image: str = Field(default="forge-agent:dev", min_length=1)
    cpus: float = Field(default=1.0, gt=0, le=8)
    memory: str = Field(default="1g", pattern=r"^[1-9][0-9]*(?:m|g)$")
    pids_limit: int = Field(default=128, gt=0, le=1024)
    timeout_seconds: float = Field(default=900, gt=0, le=3600)

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("sandbox workspace must be a directory")
        if "," in str(resolved):
            raise ValueError("Docker --mount source cannot contain a comma")
        return resolved


class SandboxResult(BaseModel):
    """Bounded Docker process outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


class DockerSandbox:
    """Build and optionally run the exact hardened Docker command."""

    def __init__(
        self,
        config: DockerSandboxConfig,
        *,
        docker_executable: str | None = None,
    ) -> None:
        self.config = config
        self._docker = docker_executable or shutil.which("docker")

    @property
    def available(self) -> bool:
        return self._docker is not None

    def build_command(
        self,
        target_command: tuple[str, ...],
        *,
        container_name: str | None = None,
        working_directory: str = ".",
    ) -> list[str]:
        """Return an argv list with no privileged or broad host mounts."""
        if not target_command:
            raise ValueError("sandbox target command cannot be empty")
        relative_workdir = PurePosixPath(working_directory)
        if relative_workdir.is_absolute() or ".." in relative_workdir.parts:
            raise ValueError("sandbox working directory must stay inside the workspace")
        container_workdir = str(PurePosixPath("/workspace") / relative_workdir)
        docker = self._docker or "docker"
        command = [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.config.pids_limit),
            "--memory",
            self.config.memory,
            "--cpus",
            str(self.config.cpus),
            "--user",
            "10001:10001",
            "--mount",
            f"type=bind,source={self.config.workspace},target=/workspace",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--workdir",
            container_workdir,
        ]
        if container_name is not None:
            command.extend(("--name", container_name))
        command.extend(
            (
                "--entrypoint",
                target_command[0],
                self.config.image,
                *target_command[1:],
            )
        )
        return command

    def run(
        self,
        target_command: tuple[str, ...],
        *,
        timeout_seconds: float | None = None,
        working_directory: str = ".",
    ) -> SandboxResult:
        """Run Docker with an outer host timeout and bounded output."""
        if self._docker is None:
            raise SandboxUnavailableError("Docker is not installed; see docs/DOCKER_SETUP.md")
        container_name = f"forge-agent-{uuid.uuid4().hex}"
        command = self.build_command(
            target_command,
            container_name=container_name,
            working_directory=working_directory,
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds or self.config.timeout_seconds,
                shell=False,
                creationflags=WINDOWS_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as error:
            self._force_remove(container_name)
            return SandboxResult(
                exit_code=-1,
                stdout=(error.stdout or "") if isinstance(error.stdout, str) else "",
                stderr=(error.stderr or "") if isinstance(error.stderr, str) else "",
                timed_out=True,
            )
        except OSError as error:
            raise SandboxUnavailableError(
                f"Unable to start Docker: {type(error).__name__}"
            ) from error
        return SandboxResult(
            exit_code=completed.returncode,
            stdout=completed.stdout[-100_000:],
            stderr=completed.stderr[-100_000:],
            timed_out=False,
        )

    def _force_remove(self, container_name: str) -> None:
        """Best-effort cleanup when the Docker client times out."""
        if self._docker is None:
            return
        try:
            subprocess.run(
                [self._docker, "rm", "--force", container_name],
                check=False,
                capture_output=True,
                timeout=15,
                shell=False,
                creationflags=WINDOWS_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return


def configured_docker_sandbox(
    settings: ForgeSettings,
    *,
    workspace: Path | None = None,
) -> DockerSandbox | None:
    """Build the single Docker boundary used by CLI and evaluation entry points."""
    if settings.execution.backend == "local":
        return None
    return DockerSandbox(
        DockerSandboxConfig(
            workspace=workspace or settings.workspace.allowed_root,
            image=settings.execution.docker_image,
            cpus=settings.execution.docker_cpus,
            memory=settings.execution.docker_memory,
            pids_limit=settings.execution.docker_pids_limit,
            timeout_seconds=float(settings.agent.timeout_seconds),
        )
    )
