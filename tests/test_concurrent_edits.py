"""Read-version and transaction conflict behavior across independent writers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from forge_agent.context import ContextManager
from forge_agent.domain import ToolCall, ToolResult
from forge_agent.tools import ApplyPatchTool, ReadFileTool, Workspace


def test_conflict_allows_fresh_read_instead_of_false_no_progress(tmp_path: Path) -> None:
    context = ContextManager(Workspace(tmp_path), repeated_call_limit=2)
    read = ToolCall(id="read", name="read_file", arguments={"path": "app.py"})
    assert context.before_tool(read) is None
    patch = ToolCall(
        id="patch",
        name="apply_patch",
        arguments={
            "edits": [
                {"path": "app.py", "old_text": "a", "new_text": "b"},
            ]
        },
    )
    context.process_result(
        patch,
        ToolResult(
            call_id=patch.id,
            name=patch.name,
            ok=False,
            error="changed",
            metadata={"error_code": "stale_read"},
        ),
    )
    assert context.before_tool(read) is None


def test_read_version_rejects_change_outside_replacement(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("VALUE = 1\nOTHER = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    read = ReadFileTool(workspace).execute(
        ToolCall(id="read", name="read_file", arguments={"path": "app.py"})
    )
    assert read.metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text("VALUE = 1\nOTHER = 99\n", encoding="utf-8")
    call = ToolCall(
        id="edit",
        name="apply_patch",
        arguments={"edits": [{"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}]},
    )
    result = ApplyPatchTool(workspace, require_read=True).execute(call)
    assert not result.ok
    assert result.metadata["error_code"] == "stale_read"
    assert path.read_text(encoding="utf-8") == "VALUE = 1\nOTHER = 99\n"
    ReadFileTool(workspace).execute(
        ToolCall(id="reread", name="read_file", arguments={"path": "app.py"})
    )
    assert ApplyPatchTool(workspace, require_read=True).execute(call).ok
    assert path.read_text(encoding="utf-8") == "VALUE = 2\nOTHER = 99\n"


def test_model_cannot_bypass_required_read_by_supplying_hash(tmp_path: Path) -> None:
    path = tmp_path / "app.py"
    path.write_text("old", encoding="utf-8")
    result = ApplyPatchTool(Workspace(tmp_path), require_read=True).execute(
        ToolCall(
            id="edit",
            name="apply_patch",
            arguments={
                "edits": [
                    {
                        "path": "app.py",
                        "old_text": "old",
                        "new_text": "new",
                        "base_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                ]
            },
        )
    )
    assert result.metadata["error_code"] == "read_required"
    assert path.read_text(encoding="utf-8") == "old"


def test_explicit_base_version_is_checked_in_compatibility_mode(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("old", encoding="utf-8")
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        ToolCall(
            id="edit",
            name="apply_patch",
            arguments={
                "edits": [
                    {
                        "path": "app.py",
                        "old_text": "old",
                        "new_text": "new",
                        "base_sha256": "0" * 64,
                    }
                ]
            },
        )
    )
    assert result.metadata["error_code"] == "stale_read"


def test_failed_batch_preserves_external_edit_during_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.write_text("old", encoding="utf-8")
    second.write_text("old", encoding="utf-8")
    replace = os.replace

    def competing_replace(src: object, dst: object) -> None:
        if dst == second:
            first.write_text("human edit", encoding="utf-8")
            raise OSError("injected failure")
        replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("forge_agent.tools.patch.os.replace", competing_replace)
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        ToolCall(
            id="edit",
            name="apply_patch",
            arguments={
                "edits": [
                    {"path": "a.py", "old_text": "old", "new_text": "new"},
                    {"path": "b.py", "old_text": "old", "new_text": "new"},
                ]
            },
        )
    )
    assert not result.ok
    assert result.metadata["rollback_conflicts"] == ["a.py"]
    assert first.read_text(encoding="utf-8") == "human edit"
    assert second.read_text(encoding="utf-8") == "old"


def test_new_file_creation_never_replaces_competing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new.py"
    link = os.link

    def competing_link(src: object, dst: object) -> None:
        target.write_text("human", encoding="utf-8")
        link(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("forge_agent.tools.patch.os.link", competing_link)
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        ToolCall(
            id="edit",
            name="apply_patch",
            arguments={
                "edits": [
                    {"path": "new.py", "new_text": "agent"},
                ]
            },
        )
    )
    assert not result.ok
    assert target.read_text(encoding="utf-8") == "human"


def test_disappearing_file_during_verification_rolls_back_only_our_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = tmp_path / "a.py", tmp_path / "b.py"
    first.write_text("old", encoding="utf-8")
    second.write_text("old", encoding="utf-8")
    read_bytes = Path.read_bytes

    def disappearing_read(path: Path) -> bytes:
        content = read_bytes(path)
        if path == second and content == b"new":
            second.unlink()
            raise FileNotFoundError("another writer removed the file")
        return content

    monkeypatch.setattr(Path, "read_bytes", disappearing_read)
    result = ApplyPatchTool(Workspace(tmp_path)).execute(
        ToolCall(
            id="edit",
            name="apply_patch",
            arguments={
                "edits": [
                    {"path": "a.py", "old_text": "old", "new_text": "new"},
                    {"path": "b.py", "old_text": "old", "new_text": "new"},
                ]
            },
        )
    )
    assert not result.ok
    assert result.metadata["error_code"] == "patch_verification_error"
    assert result.metadata["rollback_conflicts"] == ["b.py"]
    assert first.read_text(encoding="utf-8") == "old"
    assert not second.exists()
