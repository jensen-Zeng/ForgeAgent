from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import forge_agent.worktrees as worktrees
from forge_agent.worktrees import WorktreeError, WorktreeManager


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=False,
    )
    return completed.stdout.strip()


def _repository(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Forge Test")
    _git(path, "config", "user.email", "forge-test@example.invalid")
    (path / "hello.txt").write_text("base\n", encoding="utf-8")
    (path / ".gitignore").write_text(".forge/\n", encoding="utf-8")
    _git(path, "add", "hello.txt", ".gitignore")
    _git(path, "commit", "--quiet", "-m", "initial")
    return path


def test_create_produces_independent_checkouts_and_keeps_source(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo with spaces")
    manager = WorktreeManager(source)

    first = manager.create()
    second = manager.create()

    assert first.path != second.path
    assert first.branch != second.branch
    assert first.base_commit == second.base_commit
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 3
    (source / "hello.txt").write_text("source edit\n", encoding="utf-8")
    assert (first.path / "hello.txt").read_text(encoding="utf-8") == "base\n"
    assert (second.path / "hello.txt").read_text(encoding="utf-8") == "base\n"
    assert {entry.id for entry in manager.list()} == {first.id, second.id}


def test_create_rejects_dirty_source_and_non_repository(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo")
    manager = WorktreeManager(source)
    (source / "hello.txt").write_text("modified\n", encoding="utf-8")
    with pytest.raises(WorktreeError) as dirty:
        manager.create()
    assert dirty.value.code == "workspace_dirty"

    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    with pytest.raises(WorktreeError) as outside:
        WorktreeManager(non_repo)
    assert outside.value.code == "not_git_repository"


def test_manager_and_empty_list_do_not_create_storage(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo")
    manager = WorktreeManager(source)
    assert not (source / ".forge").exists()
    assert manager.list() == ()
    assert not (source / ".forge").exists()


def test_tracked_changes_under_forge_are_still_dirty(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo")
    metadata = source / ".forge"
    metadata.mkdir()
    (metadata / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "-f", ".forge/tracked.txt")
    _git(source, "commit", "--quiet", "-m", "tracked forge data")
    (metadata / "tracked.txt").write_text("modified\n", encoding="utf-8")

    with pytest.raises(WorktreeError) as error:
        WorktreeManager(source).create()

    assert error.value.code == "workspace_dirty"


def test_parallel_create_allocates_distinct_worktrees(tmp_path: Path) -> None:
    source = _repository(tmp_path / "parallel")
    manager = WorktreeManager(source)
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = tuple(pool.map(lambda _: manager.create(), range(4)))
    assert len({item.id for item in created}) == 4
    assert len({item.branch for item in created}) == 4
    assert len(manager.list()) == 4


def test_manifest_tampering_is_rejected(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo")
    manager = WorktreeManager(source)
    workspace = manager.create()
    manifest = source / ".forge" / "worktrees" / workspace.id / "manifest.json"
    content = manifest.read_text(encoding="utf-8").replace(workspace.branch, "forge/attacker")
    manifest.write_text(content, encoding="utf-8")
    with pytest.raises(WorktreeError) as error:
        manager.get(workspace.id)
    assert error.value.code == "manifest_invalid"


def test_diff_includes_new_text_and_excludes_secret_files(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo")
    manager = WorktreeManager(source)
    workspace = manager.create()
    (workspace.path / "hello.txt").write_text("changed\n", encoding="utf-8")
    (workspace.path / "新文件.txt").write_text("new content\n", encoding="utf-8")
    (workspace.path / ".env.local").write_text("TOKEN=never-show\n", encoding="utf-8")
    (workspace.path / ".netrc").write_text(
        "machine private login user password never-show\n", encoding="utf-8"
    )
    _git(workspace.path, "add", "hello.txt")

    result = manager.diff(workspace.id)

    assert "changed" in result
    assert "新文件.txt" in result
    assert "new content" in result
    assert "TOKEN=never-show" not in result
    assert ".env.local" not in result
    assert "never-show" not in result
    assert ".netrc" not in result


def test_diff_ignores_symlinked_untracked_files(tmp_path: Path) -> None:
    source = _repository(tmp_path / "repo")
    outside = tmp_path / "outside.txt"
    outside.write_text("private outside content\n", encoding="utf-8")
    workspace = WorktreeManager(source).create()
    try:
        (workspace.path / "linked.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    assert "private outside content" not in WorktreeManager(source).diff(workspace.id)


def test_create_rejects_configured_filter_without_running_it(tmp_path: Path) -> None:
    source = _repository(tmp_path / "filter-create")
    (source / ".gitattributes").write_text("hello.txt filter=custom\n", encoding="utf-8")
    _git(source, "add", ".gitattributes")
    _git(source, "commit", "--quiet", "-m", "add filter attribute")
    _git(
        source, "config", "filter.custom.smudge", "python -c \"raise RuntimeError('must not run')\""
    )

    with pytest.raises(WorktreeError) as error:
        WorktreeManager(source).create()

    assert error.value.code == "checkout_filter_unsupported"
    assert _git(source, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_diff_rejects_filter_added_after_checkout(tmp_path: Path) -> None:
    source = _repository(tmp_path / "filter-diff")
    manager = WorktreeManager(source)
    workspace = manager.create()
    (workspace.path / ".gitattributes").write_text("hello.txt filter=custom\n", encoding="utf-8")
    (workspace.path / "hello.txt").write_text("changed\n", encoding="utf-8")
    _git(
        source, "config", "filter.custom.clean", "python -c \"raise RuntimeError('must not run')\""
    )

    with pytest.raises(WorktreeError) as error:
        manager.diff(workspace.id)

    assert error.value.code == "checkout_filter_unsupported"


def test_diff_filter_preflight_runs_before_path_discovery(tmp_path: Path) -> None:
    source = _repository(tmp_path / "filter-preflight")
    manager = WorktreeManager(source)
    workspace = manager.create()
    sentinel = tmp_path / "filter-was-executed.txt"
    script = tmp_path / "filter.py"
    script.write_text(
        "import pathlib, sys\n"
        f"pathlib.Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    (workspace.path / ".gitattributes").write_text("hello.txt filter=custom\n", encoding="utf-8")
    (workspace.path / "hello.txt").write_text("changed\n", encoding="utf-8")
    _git(
        source,
        "config",
        "filter.custom.clean",
        f'"{sys.executable}" "{script}"',
    )

    with pytest.raises(WorktreeError) as error:
        manager.diff(workspace.id)

    assert error.value.code == "checkout_filter_unsupported"
    assert not sentinel.exists()


def test_untracked_diff_caps_bytes_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "large.txt"
    path.write_bytes(b"0123456789abcdef")
    monkeypatch.setattr(worktrees, "_MAX_FILE_BYTES", 8)

    assert worktrees._untracked_diff(path, "large.txt") == ""
