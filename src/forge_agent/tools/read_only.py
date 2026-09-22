"""Read-only tools for listing, reading, and searching a code workspace."""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge_agent.tools.base import BaseTool, ToolError, ToolExecution
from forge_agent.tools.workspace import Workspace, WorkspaceError

_DEFAULT_READ_LIMIT = 20_000
_MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024


class ToolParameters(BaseModel):
    """Strict base for model-supplied tool arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ListFilesParameters(ToolParameters):
    """Arguments for listing a bounded directory tree."""

    path: str = "."
    max_depth: int = Field(default=4, ge=0, le=20)
    max_entries: int = Field(default=500, gt=0, le=5_000)


class ListFilesTool(BaseTool[ListFilesParameters]):
    """List safe workspace entries in deterministic order."""

    name = "list_files"
    description = "List files and directories inside the allowed workspace."
    parameters_type: ClassVar[type[BaseModel]] = ListFilesParameters

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def run(self, parameters: ListFilesParameters) -> ToolExecution:
        try:
            start = self._workspace.resolve(parameters.path)
        except WorkspaceError as error:
            raise ToolError(str(error), code=error.code) from error
        entries: list[str] = []
        truncated = False
        if start.is_file():
            entries.append(self._workspace.display_path(start))
        else:
            self._walk(
                start,
                depth=0,
                max_depth=parameters.max_depth,
                max_entries=parameters.max_entries + 1,
                entries=entries,
            )
            truncated = len(entries) > parameters.max_entries
            entries = entries[: parameters.max_entries]
        output = "\n".join(entries) if entries else "(no visible entries)"
        return ToolExecution(
            output=output,
            metadata={
                "path": self._workspace.display_path(start),
                "count": len(entries),
                "max_depth": parameters.max_depth,
                "truncated": truncated,
            },
        )

    def _walk(
        self,
        directory: Path,
        *,
        depth: int,
        max_depth: int,
        max_entries: int,
        entries: list[str],
    ) -> None:
        if depth > max_depth or len(entries) >= max_entries:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
        except OSError as error:
            raise ToolError(
                f"Unable to list {self._workspace.display_path(directory)}: {error}",
                code="filesystem_error",
            ) from error
        for child in children:
            if len(entries) >= max_entries:
                return
            if child.is_symlink():
                continue
            if not self._workspace.is_within_root(child):
                continue
            if self._workspace.is_ignored(child) or self._workspace.is_sensitive(child):
                continue
            display = self._workspace.display_path(child)
            if child.is_dir():
                entries.append(f"{display}/")
                if depth < max_depth:
                    self._walk(
                        child,
                        depth=depth + 1,
                        max_depth=max_depth,
                        max_entries=max_entries,
                        entries=entries,
                    )
            elif child.is_file():
                entries.append(display)


class ReadFileParameters(ToolParameters):
    """Arguments for reading a UTF-8 text file with line bounds."""

    path: str = Field(min_length=1)
    start_line: int = Field(default=1, gt=0)
    end_line: int | None = Field(default=None, gt=0)
    max_chars: int = Field(default=_DEFAULT_READ_LIMIT, gt=0, le=100_000)

    @model_validator(mode="after")
    def validate_line_range(self) -> ReadFileParameters:
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot be lower than start_line")
        return self


class ReadFileTool(BaseTool[ReadFileParameters]):
    """Read a safe UTF-8 text file and annotate output with line numbers."""

    name = "read_file"
    description = "Read a line range from a UTF-8 text file inside the workspace."
    parameters_type: ClassVar[type[BaseModel]] = ReadFileParameters

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def run(self, parameters: ReadFileParameters) -> ToolExecution:
        try:
            path = self._workspace.resolve(parameters.path, require_file=True)
        except WorkspaceError as error:
            raise ToolError(str(error), code=error.code) from error
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise ToolError(
                f"Unable to read {self._workspace.display_path(path)}: {error}",
                code="filesystem_error",
            ) from error
        if b"\x00" in raw[:8192]:
            raise ToolError(
                f"Binary file is not readable: {self._workspace.display_path(path)}",
                code="binary_file",
            )
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ToolError(
                f"File is not valid UTF-8 text: {self._workspace.display_path(path)}",
                code="invalid_encoding",
            ) from error
        lines = text.splitlines()
        if parameters.start_line > max(len(lines), 1):
            raise ToolError(
                f"start_line {parameters.start_line} exceeds file length {len(lines)}",
                code="line_range_out_of_bounds",
            )
        end_line = min(parameters.end_line or len(lines), len(lines))
        selected = lines[parameters.start_line - 1 : end_line]
        rendered = "\n".join(
            f"{line_number:>6} | {line}"
            for line_number, line in enumerate(selected, start=parameters.start_line)
        )
        truncated = len(rendered) > parameters.max_chars
        if truncated:
            rendered = rendered[: parameters.max_chars] + "\n...[truncated]"
        return ToolExecution(
            output=rendered,
            metadata={
                "path": self._workspace.display_path(path),
                "start_line": parameters.start_line,
                "end_line": end_line,
                "total_lines": len(lines),
                "truncated": truncated,
            },
        )


class SearchCodeParameters(ToolParameters):
    """Arguments for fixed-string content or filename search."""

    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    mode: Literal["content", "filename"] = "content"
    file_glob: str | None = None
    case_sensitive: bool = False
    max_results: int = Field(default=100, gt=0, le=1_000)


class SearchCodeTool(BaseTool[SearchCodeParameters]):
    """Search with ripgrep when available and a safe Python fallback."""

    name = "search_code"
    description = "Search file contents or filenames inside the allowed workspace."
    parameters_type: ClassVar[type[BaseModel]] = SearchCodeParameters

    def __init__(
        self,
        workspace: Workspace,
        *,
        prefer_rg: bool = True,
        rg_executable: str | None = None,
    ) -> None:
        self._workspace = workspace
        self._rg_executable = rg_executable or shutil.which("rg") if prefer_rg else None

    def run(self, parameters: SearchCodeParameters) -> ToolExecution:
        try:
            start = self._workspace.resolve(parameters.path)
        except WorkspaceError as error:
            raise ToolError(str(error), code=error.code) from error
        if self._rg_executable is not None:
            try:
                matches = self._search_with_rg(parameters, start)
                backend = "rg"
            except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
                matches = self._search_with_python(parameters, start)
                backend = "python_fallback"
        else:
            matches = self._search_with_python(parameters, start)
            backend = "python"
        truncated = len(matches) > parameters.max_results
        visible_matches = matches[: parameters.max_results]
        return ToolExecution(
            output="\n".join(visible_matches) if visible_matches else "(no matches)",
            metadata={
                "path": self._workspace.display_path(start),
                "mode": parameters.mode,
                "count": len(visible_matches),
                "truncated": truncated,
                "backend": backend,
            },
        )

    def _search_with_rg(
        self,
        parameters: SearchCodeParameters,
        start: Path,
    ) -> list[str]:
        relative_start = self._workspace.display_path(start)
        if parameters.mode == "filename":
            command = [self._rg_executable or "rg", "--files", "--", relative_start]
        else:
            command = [
                self._rg_executable or "rg",
                "--json",
                "--fixed-strings",
                "--line-number",
                "--column",
                "--color",
                "never",
            ]
            if not parameters.case_sensitive:
                command.append("--ignore-case")
            if parameters.file_glob:
                command.extend(("--glob", parameters.file_glob))
            command.extend(("--", parameters.query, relative_start))
        completed = subprocess.run(
            command,
            cwd=self._workspace.root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            shell=False,
        )
        if completed.returncode not in (0, 1):
            raise subprocess.SubprocessError(f"rg exited with code {completed.returncode}")
        if parameters.mode == "filename":
            return self._filter_filename_matches(completed.stdout.splitlines(), parameters)
        return self._parse_rg_json(completed.stdout, parameters.max_results + 1)

    def _parse_rg_json(self, output: str, limit: int) -> list[str]:
        matches: list[str] = []
        for line in output.splitlines():
            record = json.loads(line)
            if record.get("type") != "match":
                continue
            data = record["data"]
            relative_path = Path(data["path"]["text"])
            absolute_path = (self._workspace.root / relative_path).resolve(strict=False)
            if (
                not self._workspace.is_within_root(absolute_path)
                or self._workspace.is_ignored(absolute_path)
                or self._workspace.is_sensitive(absolute_path)
            ):
                continue
            line_number = int(data["line_number"])
            line_text = str(data["lines"]["text"]).rstrip("\r\n")
            submatches = data.get("submatches", [])
            column = int(submatches[0]["start"]) + 1 if submatches else 1
            display = self._workspace.display_path(absolute_path)
            matches.append(f"{display}:{line_number}:{column}: {line_text}")
            if len(matches) >= limit:
                break
        return matches

    def _filter_filename_matches(
        self,
        candidates: list[str],
        parameters: SearchCodeParameters,
    ) -> list[str]:
        matches: list[str] = []
        needle = parameters.query if parameters.case_sensitive else parameters.query.casefold()
        for candidate_text in candidates:
            candidate = (self._workspace.root / candidate_text).resolve(strict=False)
            if (
                not self._workspace.is_within_root(candidate)
                or self._workspace.is_ignored(candidate)
                or self._workspace.is_sensitive(candidate)
            ):
                continue
            display = self._workspace.display_path(candidate)
            comparable = display if parameters.case_sensitive else display.casefold()
            if needle not in comparable:
                continue
            if parameters.file_glob and not fnmatch.fnmatch(display, parameters.file_glob):
                continue
            matches.append(display)
            if len(matches) > parameters.max_results:
                break
        return matches

    def _search_with_python(
        self,
        parameters: SearchCodeParameters,
        start: Path,
    ) -> list[str]:
        matches: list[str] = []
        needle = parameters.query if parameters.case_sensitive else parameters.query.casefold()
        for path in self._workspace.iter_files(start):
            display = self._workspace.display_path(path)
            if parameters.file_glob and not fnmatch.fnmatch(display, parameters.file_glob):
                continue
            if parameters.mode == "filename":
                comparable = display if parameters.case_sensitive else display.casefold()
                if needle in comparable:
                    matches.append(display)
            else:
                matches.extend(self._search_text_file(path, display, needle, parameters))
            if len(matches) > parameters.max_results:
                break
        return matches

    def _search_text_file(
        self,
        path: Path,
        display: str,
        needle: str,
        parameters: SearchCodeParameters,
    ) -> list[str]:
        try:
            if path.stat().st_size > _MAX_SEARCH_FILE_BYTES:
                return []
            raw = path.read_bytes()
        except OSError:
            return []
        if b"\x00" in raw[:8192]:
            return []
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return []
        matches: list[str] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            comparable = line if parameters.case_sensitive else line.casefold()
            column = comparable.find(needle)
            if column < 0:
                continue
            matches.append(f"{display}:{line_number}:{column + 1}: {line}")
            if len(matches) > parameters.max_results:
                break
        return matches
