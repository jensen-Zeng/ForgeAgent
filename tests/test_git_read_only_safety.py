"""Security regression tests for read-only Git inspection tools."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from forge_agent.domain import ToolCall
from forge_agent.tools import GitDiffTool, GitStatusTool, Workspace


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        shell=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def init_repo(root: Path, *, tracked_file: str = "source.txt") -> Path:
    git(root, "init", "-q")
    git(root, "config", "user.name", "Forge Test")
    git(root, "config", "user.email", "forge-test@example.invalid")
    path = root / tracked_file
    path.write_text("before\n", encoding="utf-8")
    git(root, "add", tracked_file)
    git(root, "commit", "-qm", "baseline")
    return path


def call(name: str) -> ToolCall:
    return ToolCall(id="test-call", name=name, arguments={})


def command_for(script: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def write_sentinel_script(path: Path, marker: Path) -> None:
    path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )


def test_status_and_diff_disable_configured_fsmonitor(tmp_path: Path) -> None:
    init_repo(tmp_path)
    marker = tmp_path / "fsmonitor-executed"
    hook = tmp_path / "fsmonitor.py"
    write_sentinel_script(hook, marker)
    git(tmp_path, "config", "core.fsmonitor", command_for(hook))
    workspace = Workspace(tmp_path)

    status = GitStatusTool(workspace).execute(call("git_status"))
    diff = GitDiffTool(workspace).execute(call("git_diff"))

    assert status.ok is True
    assert diff.ok is True
    assert not marker.exists()


def test_diff_disables_textconv_command(tmp_path: Path) -> None:
    tracked = init_repo(tmp_path)
    git(tmp_path, "config", "diff.sentinel.textconv", command_for(tmp_path / "textconv.py"))
    marker = tmp_path / "textconv-executed"
    write_sentinel_script(tmp_path / "textconv.py", marker)
    (tmp_path / ".gitattributes").write_text("source.txt diff=sentinel\n", encoding="utf-8")
    git(tmp_path, "add", ".gitattributes")
    git(tmp_path, "commit", "-qm", "configure textconv attribute")
    tracked.write_text("after\n", encoding="utf-8")

    diff = GitDiffTool(Workspace(tmp_path)).execute(call("git_diff"))

    assert diff.ok is True
    assert "-before" in diff.output
    assert "+after" in diff.output
    assert not marker.exists()


def test_applied_configured_clean_filter_is_refused_without_execution(tmp_path: Path) -> None:
    init_repo(tmp_path)
    attributes = tmp_path / ".gitattributes"
    attributes.write_text("source.txt filter=sentinel\n", encoding="utf-8")
    git(tmp_path, "add", ".gitattributes")
    git(tmp_path, "commit", "-qm", "configure filter attribute")
    marker = tmp_path / "filter-executed"
    script = tmp_path / "filter.py"
    write_sentinel_script(script, marker)
    git(tmp_path, "config", "filter.sentinel.clean", command_for(script))
    workspace = Workspace(tmp_path)

    status = GitStatusTool(workspace).execute(call("git_status"))
    diff = GitDiffTool(workspace).execute(call("git_diff"))

    assert status.metadata["error_code"] == "git_filter_unsupported"
    assert diff.metadata["error_code"] == "git_filter_unsupported"
    assert not marker.exists()


def test_nested_workspace_detects_filter_on_tracked_path_outside_scope(tmp_path: Path) -> None:
    init_repo(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside\n", encoding="utf-8")
    git(tmp_path, "add", "nested/inside.txt")
    attributes = tmp_path / ".gitattributes"
    attributes.write_text("source.txt filter=root-only\n", encoding="utf-8")
    git(tmp_path, "add", ".gitattributes")
    git(tmp_path, "commit", "-qm", "configure root-only filter")
    marker = tmp_path / "root-filter-executed"
    script = tmp_path / ".git" / "root-filter.py"
    write_sentinel_script(script, marker)
    git(tmp_path, "config", "filter.root-only.clean", command_for(script))

    status = GitStatusTool(Workspace(nested)).execute(call("git_status"))
    diff = GitDiffTool(Workspace(nested)).execute(call("git_diff"))

    assert status.metadata["error_code"] == "git_filter_unsupported"
    assert diff.metadata["error_code"] == "git_filter_unsupported"
    assert not marker.exists()


def test_unused_configured_filter_does_not_disable_git_status(tmp_path: Path) -> None:
    init_repo(tmp_path)
    marker = tmp_path / ".git" / "unused-filter-executed"
    script = tmp_path / ".git" / "unused-filter.py"
    write_sentinel_script(script, marker)
    git(tmp_path, "config", "filter.unused.clean", command_for(script))

    status = GitStatusTool(Workspace(tmp_path)).execute(call("git_status"))

    assert status.ok is True
    assert status.output == "(working tree clean)"
    assert not marker.exists()


def test_status_and_diff_still_report_real_changes(tmp_path: Path) -> None:
    tracked = init_repo(tmp_path)
    tracked.write_text("after\n", encoding="utf-8")

    status = GitStatusTool(Workspace(tmp_path)).execute(call("git_status"))
    diff = GitDiffTool(Workspace(tmp_path)).execute(call("git_diff"))

    assert status.ok is True
    assert "source.txt" in status.output
    assert diff.ok is True
    assert "-before" in diff.output
    assert "+after" in diff.output
