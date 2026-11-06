"""Read-only Git status and diff tools with bounded output."""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from forge_agent.tools.base import BaseTool, ToolError, ToolExecution
from forge_agent.tools.workspace import Workspace


class GitStatusParameters(BaseModel):
    """No-argument status request."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GitDiffParameters(BaseModel):
    """Options for a bounded working-tree diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    staged: bool = False
    paths: tuple[str, ...] = Field(default=(), max_length=50)
    max_chars: int = Field(default=50_000, gt=0, le=200_000)


class _GitTool:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def _run_git(
        self,
        arguments: list[str],
        *,
        expected_returncodes: tuple[int, ...] = (0,),
        input_text: str | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "diff.external=",
                    "-C",
                    str(cwd or self._workspace.root),
                    *arguments,
                ],
                check=False,
                capture_output=True,
                input=input_text,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                shell=False,
            )
        except FileNotFoundError as error:
            raise ToolError("Git executable is unavailable", code="git_unavailable") from error
        except subprocess.TimeoutExpired as error:
            raise ToolError("Git command timed out", code="git_timeout") from error
        if completed.returncode not in expected_returncodes:
            error_text = completed.stderr.strip()
            if "not a git repository" in error_text.casefold():
                raise ToolError(
                    "Workspace is not inside a Git repository",
                    code="not_git_repository",
                )
            raise ToolError(
                f"Git command failed with exit code {completed.returncode}",
                code="git_error",
            )
        return completed

    def _ensure_no_applied_filters(self) -> None:
        root_result = self._run_git(["rev-parse", "--show-toplevel"])
        try:
            git_root = Path(root_result.stdout.strip()).resolve(strict=True)
            self._workspace.root.relative_to(git_root)
        except (OSError, ValueError) as error:
            raise ToolError(
                "Unable to resolve the Git root for safe filter inspection",
                code="git_error",
            ) from error
        tracked = tuple(
            path
            for path in self._run_git(["ls-files", "-z"], cwd=git_root).stdout.split("\0")
            if path
        )
        if not tracked:
            return
        attributes = self._run_git(
            ["check-attr", "-z", "filter", "--stdin"],
            input_text="".join(f"{path}\0" for path in tracked),
            cwd=git_root,
        ).stdout.split("\0")
        active_drivers = {
            attributes[index + 2].casefold()
            for index in range(0, len(attributes) - 2, 3)
            if attributes[index + 1] == "filter"
            and attributes[index + 2] not in {"", "unspecified", "unset", "set"}
        }
        if not active_drivers:
            return
        completed = self._run_git(
            [
                "config",
                "--null",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process)$",
            ],
            expected_returncodes=(0, 1),
            cwd=git_root,
        )
        if completed.returncode == 1:
            return
        configured_drivers: set[str] = set()
        for entry in completed.stdout.split("\0"):
            if "\n" not in entry:
                continue
            key, value = entry.split("\n", 1)
            match = re.fullmatch(r"filter\.(.+)\.(?:clean|smudge|process)", key)
            if match and value.strip():
                configured_drivers.add(match.group(1).casefold())
        unsupported = sorted(active_drivers & configured_drivers)
        if unsupported:
            raise ToolError(
                "Git filter(s) are configured for tracked files; read-only Git inspection "
                "is disabled for this repository",
                code="git_filter_unsupported",
            )

    def _validate_paths(self, paths: tuple[str, ...]) -> tuple[str, ...]:
        validated: list[str] = []
        for raw in paths:
            candidate = (self._workspace.root / raw).resolve(strict=False)
            if not self._workspace.is_within_root(candidate):
                raise ToolError(
                    f"Git path escapes workspace: {raw}",
                    code="path_outside_workspace",
                )
            validated.append(raw)
        return tuple(validated)


class GitStatusTool(_GitTool, BaseTool[GitStatusParameters]):
    """Return stable porcelain status scoped to the workspace."""

    name = "git_status"
    description = "Show working-tree changes inside the allowed Git workspace."
    parameters_type: ClassVar[type[BaseModel]] = GitStatusParameters

    def run(self, parameters: GitStatusParameters) -> ToolExecution:
        del parameters
        self._ensure_no_applied_filters()
        completed = self._run_git(
            [
                "status",
                "--ignore-submodules=all",
                "--short",
                "--untracked-files=all",
                "--",
                ".",
            ]
        )
        output = completed.stdout.rstrip() or "(working tree clean)"
        count = len(completed.stdout.splitlines())
        return ToolExecution(output=output, metadata={"entry_count": count})


class GitDiffTool(_GitTool, BaseTool[GitDiffParameters]):
    """Return tracked and untracked text changes without modifying Git state."""

    name = "git_diff"
    description = "Show a bounded diff for working-tree or staged changes."
    parameters_type: ClassVar[type[BaseModel]] = GitDiffParameters

    def run(self, parameters: GitDiffParameters) -> ToolExecution:
        self._ensure_no_applied_filters()
        paths = self._validate_paths(parameters.paths)
        arguments = [
            "-c",
            "core.quotepath=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--ignore-submodules=all",
            "--no-color",
        ]
        if parameters.staged:
            arguments.append("--cached")
        arguments.extend(("--", *(paths or (".",))))
        completed = self._run_git(arguments)
        output = completed.stdout
        if not parameters.staged:
            output += self._untracked_diff(paths)
        truncated = len(output) > parameters.max_chars
        if truncated:
            output = output[: parameters.max_chars] + "\n...[truncated]"
        if not output:
            output = "(no diff)"
        return ToolExecution(
            output=output.rstrip(),
            metadata={
                "staged": parameters.staged,
                "paths": cast(list[JsonValue], list(paths)),
                "truncated": truncated,
            },
        )

    def _untracked_diff(self, paths: tuple[str, ...]) -> str:
        completed = self._run_git(
            ["ls-files", "--others", "--exclude-standard", "-z", "--", *(paths or (".",))]
        )
        chunks: list[str] = []
        for relative in filter(None, completed.stdout.split("\0")):
            candidate = (self._workspace.root / relative).resolve(strict=False)
            if (
                not self._workspace.is_within_root(candidate)
                or self._workspace.is_sensitive(candidate)
                or not candidate.is_file()
            ):
                continue
            try:
                raw = candidate.read_bytes()
                if b"\x00" in raw[:8192]:
                    continue
                text = raw.decode("utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            chunks.append(
                "\n".join(
                    difflib.unified_diff(
                        (),
                        text.splitlines(),
                        fromfile="/dev/null",
                        tofile=f"b/{Path(relative).as_posix()}",
                        lineterm="",
                    )
                )
            )
        return ("\n" if chunks else "") + "\n".join(chunks)
