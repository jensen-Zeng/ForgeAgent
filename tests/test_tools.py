"""Tests for the tool protocol and bounded read-only workspace tools."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from forge_agent.domain import ToolCall
from forge_agent.tools import (
    BaseTool,
    ListFilesTool,
    ReadFileTool,
    SearchCodeTool,
    ToolExecution,
    ToolRegistry,
    Workspace,
)


def call(name: str, **arguments: object) -> ToolCall:
    """Create a concise tool call fixture."""
    json_arguments = cast(dict[str, JsonValue], arguments)
    return ToolCall(id="call-1", name=name, arguments=json_arguments)


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a small repository containing ignored, secret, and binary files."""
    (tmp_path / ".gitignore").write_text("ignored.txt\nbuild/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "# 示例\n\nHello ForgeAgent\nSecond line\n",
        encoding="utf-8",
    )
    source = tmp_path / "src"
    source.mkdir()
    (source / "main.py").write_text(
        'def main() -> None:\n    print("Hello ForgeAgent")\n',
        encoding="utf-8",
    )
    (source / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("secret ignored content", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "generated.py").write_text("generated = True\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=real-secret\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    return tmp_path


def test_registry_exposes_deterministic_generated_schemas(sample_repo: Path) -> None:
    workspace = Workspace(sample_repo)
    registry = ToolRegistry(
        (
            SearchCodeTool(workspace, prefer_rg=False),
            ReadFileTool(workspace),
            ListFilesTool(workspace),
        )
    )

    assert [schema.name for schema in registry.schemas] == [
        "list_files",
        "read_file",
        "search_code",
    ]
    read_schema = next(schema for schema in registry.schemas if schema.name == "read_file")
    assert read_schema.parameters["type"] == "object"
    properties = read_schema.parameters["properties"]
    assert isinstance(properties, dict)
    assert "path" in properties
    assert read_schema.parameters["additionalProperties"] is False


def test_registry_rejects_duplicate_tool_names(sample_repo: Path) -> None:
    workspace = Workspace(sample_repo)

    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry((ReadFileTool(workspace), ReadFileTool(workspace)))


def test_unknown_tool_is_a_structured_failure() -> None:
    result = ToolRegistry().execute(call("does_not_exist"))

    assert result.ok is False
    assert result.metadata["error_code"] == "unknown_tool"
    assert result.metadata["available_tools"] == []


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"path": 123},
        {"path": "README.md", "unknown": True},
        {"path": "README.md", "start_line": 0},
    ),
)
def test_invalid_arguments_are_structured_failures(
    sample_repo: Path,
    arguments: dict[str, object],
) -> None:
    tool = ReadFileTool(Workspace(sample_repo))
    tool_call = ToolCall(
        id="call-1",
        name="read_file",
        arguments=cast(dict[str, JsonValue], arguments),
    )

    result = tool.execute(tool_call)

    assert result.ok is False
    assert result.metadata["error_code"] == "invalid_arguments"
    assert result.error is not None
    assert "input_value" not in result.error


def test_unexpected_tool_exception_does_not_escape() -> None:
    class EmptyParameters(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class BrokenTool(BaseTool[EmptyParameters]):
        name = "broken"
        description = "Always fails"
        parameters_type = EmptyParameters

        def run(self, parameters: EmptyParameters) -> ToolExecution:
            del parameters
            raise RuntimeError("internal implementation detail")

    result = BrokenTool().execute(call("broken"))

    assert result.ok is False
    assert result.metadata["error_code"] == "tool_execution_error"
    assert result.error is not None
    assert "internal implementation detail" not in result.error


def test_list_files_is_sorted_and_respects_ignore_and_sensitive_rules(
    sample_repo: Path,
) -> None:
    result = ListFilesTool(Workspace(sample_repo)).execute(
        call("list_files", path=".", max_depth=4)
    )

    assert result.ok is True
    assert "README.md" in result.output
    assert "src/" in result.output
    assert "src/main.py" in result.output
    assert "ignored.txt" not in result.output
    assert "build/" not in result.output
    assert ".env" not in result.output


def test_list_files_applies_depth_and_entry_limits(sample_repo: Path) -> None:
    result = ListFilesTool(Workspace(sample_repo)).execute(
        call("list_files", max_depth=0, max_entries=2)
    )

    assert result.ok is True
    assert result.metadata["count"] == 2
    assert result.metadata["truncated"] is True
    assert "src/main.py" not in result.output


def test_read_file_returns_numbered_utf8_line_range(sample_repo: Path) -> None:
    result = ReadFileTool(Workspace(sample_repo)).execute(
        call("read_file", path="README.md", start_line=1, end_line=3)
    )

    assert result.ok is True
    assert "1 | # 示例" in result.output
    assert "3 | Hello ForgeAgent" in result.output
    assert result.metadata["total_lines"] == 4
    assert result.metadata["start_line"] == 1
    assert result.metadata["end_line"] == 3


def test_read_file_truncates_large_output(sample_repo: Path) -> None:
    result = ReadFileTool(Workspace(sample_repo)).execute(
        call("read_file", path="README.md", max_chars=10)
    )

    assert result.ok is True
    assert result.output.endswith("...[truncated]")
    assert result.metadata["truncated"] is True


@pytest.mark.parametrize(
    ("path", "error_code"),
    (
        (".env", "sensitive_path"),
        ("binary.bin", "binary_file"),
        ("missing.txt", "path_not_found"),
        ("../outside.txt", "path_outside_workspace"),
    ),
)
def test_read_file_rejects_unsafe_or_unreadable_paths(
    sample_repo: Path,
    path: str,
    error_code: str,
) -> None:
    result = ReadFileTool(Workspace(sample_repo)).execute(call("read_file", path=path))

    assert result.ok is False
    assert result.metadata["error_code"] == error_code


@pytest.mark.skipif(os.name != "nt", reason="Windows drive semantics")
def test_different_windows_drive_is_rejected(sample_repo: Path) -> None:
    current_drive = sample_repo.drive.casefold()
    other_drive = "Z:" if current_drive != "z:" else "Y:"
    result = ReadFileTool(Workspace(sample_repo)).execute(
        call("read_file", path=f"{other_drive}\\outside.txt")
    )

    assert result.ok is False
    assert result.metadata["error_code"] == "path_outside_workspace"


def test_symlink_escape_is_not_listed_or_read(sample_repo: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = sample_repo / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating symlinks is not permitted on this Windows setup")

    listed = ListFilesTool(Workspace(sample_repo)).execute(call("list_files"))
    read = ReadFileTool(Workspace(sample_repo)).execute(call("read_file", path="outside-link.txt"))

    assert "outside-link.txt" not in listed.output
    assert read.ok is False
    assert read.metadata["error_code"] == "path_outside_workspace"


def test_python_content_search_returns_locations_and_skips_ignored_files(
    sample_repo: Path,
) -> None:
    tool = SearchCodeTool(Workspace(sample_repo), prefer_rg=False)

    result = tool.execute(call("search_code", query="forgeagent", mode="content"))

    assert result.ok is True
    assert "README.md:3:7: Hello ForgeAgent" in result.output
    assert "src/main.py:2:" in result.output
    assert "ignored.txt" not in result.output
    assert result.metadata["backend"] == "python"


def test_python_filename_search_and_glob(sample_repo: Path) -> None:
    tool = SearchCodeTool(Workspace(sample_repo), prefer_rg=False)

    result = tool.execute(
        call(
            "search_code",
            query="py",
            mode="filename",
            file_glob="src/*.py",
        )
    )

    assert result.ok is True
    assert result.output.splitlines() == ["src/helper.py", "src/main.py"]


def test_search_result_limit_sets_truncated(sample_repo: Path) -> None:
    tool = SearchCodeTool(Workspace(sample_repo), prefer_rg=False)

    result = tool.execute(call("search_code", query="e", mode="content", max_results=1))

    assert result.ok is True
    assert len(result.output.splitlines()) == 1
    assert result.metadata["truncated"] is True


def test_search_falls_back_when_rg_fails(
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_rg(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="rg", timeout=15)

    monkeypatch.setattr(subprocess, "run", fail_rg)
    tool = SearchCodeTool(Workspace(sample_repo), rg_executable="rg")

    result = tool.execute(call("search_code", query="VALUE", mode="content"))

    assert result.ok is True
    assert "src/helper.py:1:1: VALUE = 42" in result.output
    assert result.metadata["backend"] == "python_fallback"


def test_rg_json_output_is_parsed_without_shell(
    sample_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    match_record = {
        "type": "match",
        "data": {
            "path": {"text": "src/main.py"},
            "lines": {"text": '    print("Hello ForgeAgent")\n'},
            "line_number": 2,
            "submatches": [{"start": 17, "end": 27}],
        },
    }

    def fake_rg(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        assert "--fixed-strings" in command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(match_record) + "\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_rg)
    tool = SearchCodeTool(Workspace(sample_repo), rg_executable="rg")

    result = tool.execute(call("search_code", query="ForgeAgent"))

    assert result.ok is True
    assert "src/main.py:2:18:" in result.output
    assert result.metadata["backend"] == "rg"
