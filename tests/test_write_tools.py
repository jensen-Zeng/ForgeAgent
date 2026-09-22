"""Safety tests for exact patching and bounded Git inspection."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from forge_agent.domain import (
    ModelResponse,
    RunConfig,
    RunStatus,
    TextPart,
    ToolCall,
    ToolCallPart,
)
from forge_agent.models import FakeModelProvider
from forge_agent.runtime import AgentRuntime
from forge_agent.tools import (
    ApplyPatchTool,
    GitDiffTool,
    GitStatusTool,
    PatchReview,
    ToolRegistry,
    Workspace,
)


def tool_call(name: str, **arguments: object) -> ToolCall:
    """Build a JSON-compatible tool call."""
    return ToolCall(
        id="call-1",
        name=name,
        arguments=cast(dict[str, JsonValue], arguments),
    )


def patch_call(*edits: dict[str, object]) -> ToolCall:
    """Build one apply_patch request."""
    return tool_call("apply_patch", edits=list(edits))


def git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a deterministic local Git command for test setup."""
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )


def init_repo(path: Path) -> None:
    """Initialize a temporary Git repository."""
    git(path, "init", "-q")
    git(path, "config", "user.name", "ForgeAgent Tests")
    git(path, "config", "user.email", "tests@example.invalid")


def test_patch_creates_and_modifies_files_with_diff_evidence(tmp_path: Path) -> None:
    (tmp_path / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    tool = ApplyPatchTool(Workspace(tmp_path))

    result = tool.execute(
        patch_call(
            {
                "path": "existing.py",
                "old_text": "VALUE = 1",
                "new_text": "VALUE = 2",
            },
            {
                "path": "new.py",
                "old_text": None,
                "new_text": 'MESSAGE = "你好"\n',
            },
        )
    )

    assert result.ok is True
    assert (tmp_path / "existing.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == 'MESSAGE = "你好"\n'
    assert "--- a/existing.py" in result.output
    assert "+++ b/new.py" in result.output
    assert result.metadata["changed_files"] == ["existing.py", "new.py"]
    assert result.metadata["verified_after_write"] is True


def test_patch_rejects_context_mismatch_without_modifying_file(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")

    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "value = 0",
                "new_text": "value = 2",
            }
        )
    )

    assert result.ok is False
    assert result.metadata["error_code"] == "patch_context_mismatch"
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_patch_rejects_ambiguous_context_and_file_emptying(tmp_path: Path) -> None:
    path = tmp_path / "data.txt"
    path.write_text("same\nsame\n", encoding="utf-8")
    tool = ApplyPatchTool(Workspace(tmp_path))

    ambiguous = tool.execute(
        patch_call({"path": "data.txt", "old_text": "same", "new_text": "changed"})
    )
    delete = tool.execute(
        patch_call(
            {
                "path": "data.txt",
                "old_text": "same\nsame\n",
                "new_text": "",
            }
        )
    )

    assert ambiguous.metadata["error_code"] == "patch_context_ambiguous"
    assert delete.metadata["error_code"] == "patch_delete_denied"
    assert path.read_text(encoding="utf-8") == "same\nsame\n"


def test_concurrent_modification_is_preserved_and_patch_is_aborted(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")

    def external_change() -> None:
        path.write_text("value = 99\n", encoding="utf-8")

    tool = ApplyPatchTool(Workspace(tmp_path), before_commit=external_change)
    result = tool.execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            }
        )
    )

    assert result.ok is False
    assert result.metadata["error_code"] == "concurrent_modification"
    assert path.read_text(encoding="utf-8") == "value = 99\n"
    assert list(tmp_path.glob(".forge-patch-*.tmp")) == []


def test_atomic_replace_failure_leaves_original_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")

    def fail_replace(
        source: str | bytes | os.PathLike[str], target: str | bytes | os.PathLike[str]
    ) -> None:
        del source, target
        raise OSError("simulated replace failure")

    monkeypatch.setattr("forge_agent.tools.patch.os.replace", fail_replace)
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            }
        )
    )

    assert result.ok is False
    assert result.metadata["error_code"] == "patch_write_error"
    assert path.read_text(encoding="utf-8") == "value = 1\n"


def test_disk_full_while_preparing_patch_leaves_original_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "app.py"
    path.write_text("value = 1\n", encoding="utf-8")

    def disk_full(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("simulated disk full")

    monkeypatch.setattr("forge_agent.tools.patch.tempfile.NamedTemporaryFile", disk_full)
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "value = 1",
                "new_text": "value = 2",
            }
        )
    )

    assert result.ok is False
    assert result.metadata["error_code"] == "patch_write_error"
    assert path.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize("target", ("../outside.py", ".env", ".git/config"))
def test_patch_rejects_outside_sensitive_and_policy_paths(
    tmp_path: Path,
    target: str,
) -> None:
    init_repo(tmp_path)
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        patch_call({"path": target, "old_text": None, "new_text": "unsafe\n"})
    )

    assert result.ok is False
    assert result.metadata["error_code"] in {
        "path_outside_workspace",
        "sensitive_path",
        "path_ignored",
    }


def test_patch_preserves_utf8_chinese_crlf_and_final_newline(tmp_path: Path) -> None:
    path = tmp_path / "说明.txt"
    path.write_bytes("第一行\r\n旧内容\r\n".encode())

    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        patch_call(
            {
                "path": "说明.txt",
                "old_text": "旧内容\n",
                "new_text": "新内容\n",
            }
        )
    )

    assert result.ok is True
    assert path.read_bytes() == "第一行\r\n新内容\r\n".encode()


def test_patch_preserves_unrelated_preexisting_user_change(tmp_path: Path) -> None:
    init_repo(tmp_path)
    path = tmp_path / "app.py"
    path.write_text("USER_VALUE = 1\nTARGET = 1\n", encoding="utf-8")
    git(tmp_path, "add", "app.py")
    git(tmp_path, "commit", "-qm", "baseline")
    path.write_text("USER_VALUE = 99\nTARGET = 1\n", encoding="utf-8")

    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "TARGET = 1",
                "new_text": "TARGET = 2",
            }
        )
    )

    assert result.ok is True
    assert path.read_text(encoding="utf-8") == "USER_VALUE = 99\nTARGET = 2\n"
    assert result.metadata["preexisting_changes"] == ["app.py"]
    assert result.metadata["workspace_status_before"] == [" M app.py"]
    assert result.metadata["reverse_patch_available"] is True


def test_patch_approval_sees_diff_before_write_and_can_reject(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    observations: list[tuple[str, bool]] = []

    def reject(review: PatchReview) -> bool:
        observations.append((review.diff, path.read_text(encoding="utf-8") == "VALUE = 1\n"))
        return False

    result = ApplyPatchTool(
        Workspace(tmp_path),
        approval_handler=reject,
        approval_mode="always",
    ).execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "VALUE = 1",
                "new_text": "VALUE = 2",
            }
        )
    )

    assert result.ok is False
    assert result.metadata["error_code"] == "approval_denied"
    assert observations[0][1] is True
    assert "+VALUE = 2" in observations[0][0]
    assert path.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_patch_approval_can_apply_reviewed_diff(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")

    result = ApplyPatchTool(
        Workspace(tmp_path),
        approval_handler=lambda review: review.changed_files == ("app.py",),
        approval_mode="always",
    ).execute(
        patch_call(
            {
                "path": "app.py",
                "old_text": "VALUE = 1",
                "new_text": "VALUE = 2",
            }
        )
    )

    assert result.ok is True
    assert path.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_patch_file_and_line_limits_are_enforced(tmp_path: Path) -> None:
    tool = ApplyPatchTool(
        Workspace(tmp_path),
        max_files=1,
        max_added_lines=1,
    )

    too_many_files = tool.execute(
        patch_call(
            {"path": "a.py", "old_text": None, "new_text": "a\n"},
            {"path": "b.py", "old_text": None, "new_text": "b\n"},
        )
    )
    too_many_lines = tool.execute(
        patch_call({"path": "c.py", "old_text": None, "new_text": "one\ntwo\n"})
    )

    assert too_many_files.metadata["error_code"] == "patch_file_limit"
    assert too_many_lines.metadata["error_code"] == "patch_added_line_limit"
    assert not (tmp_path / "c.py").exists()


def test_git_status_and_diff_cover_tracked_and_untracked_files(tmp_path: Path) -> None:
    init_repo(tmp_path)
    tracked = tmp_path / "tracked.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.py")
    git(tmp_path, "commit", "-qm", "baseline")
    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("NEW = True\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    status = GitStatusTool(workspace).execute(tool_call("git_status"))
    diff = GitDiffTool(workspace).execute(tool_call("git_diff"))

    assert status.ok is True
    assert "tracked.py" in status.output
    assert "new.py" in status.output
    assert diff.ok is True
    assert "-VALUE = 1" in diff.output
    assert "+VALUE = 2" in diff.output
    assert "+++ b/new.py" in diff.output


def test_git_tools_report_non_repository_explicitly(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    status = GitStatusTool(workspace).execute(tool_call("git_status"))
    diff = GitDiffTool(workspace).execute(tool_call("git_diff"))

    assert status.ok is False
    assert status.metadata["error_code"] == "not_git_repository"
    assert diff.ok is False
    assert diff.metadata["error_code"] == "not_git_repository"


@pytest.mark.asyncio
async def test_runtime_includes_patch_evidence_in_final_result(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    patch = patch_call(
        {
            "path": "app.py",
            "old_text": "VALUE = 1",
            "new_text": "VALUE = 2",
        }
    )
    provider = FakeModelProvider(
        (
            ModelResponse(content=(ToolCallPart(tool_call=patch),)),
            ModelResponse(content=(TextPart(text="Updated safely."),)),
        )
    )

    result = await AgentRuntime(
        provider=provider,
        tools=ToolRegistry((ApplyPatchTool(Workspace(tmp_path)),)),
    ).run(RunConfig(task="Update value", workspace=tmp_path))

    assert result.status is RunStatus.COMPLETED
    assert result.changed_files == ("app.py",)
    assert result.diff_summary is not None
    assert "+VALUE = 2" in result.diff_summary
