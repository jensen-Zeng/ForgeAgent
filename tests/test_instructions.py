"""Tests for bounded hierarchical repository instructions."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.context import ContextManager
from forge_agent.domain import RunConfig
from forge_agent.instructions import load_repository_instructions
from forge_agent.tools import ListFilesTool, ReadFileTool, Workspace
from forge_agent.tools.read_only import ListFilesParameters, ReadFileParameters


def test_precedence_and_root_to_nearest_directory_order(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root agents", encoding="utf-8")
    (tmp_path / "AGENTS.override.md").write_text("root override", encoding="utf-8")
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("nested agents", encoding="utf-8")
    (nested / "CLAUDE.md").write_text("nested claude", encoding="utf-8")

    rules = load_repository_instructions(Workspace(tmp_path), nested)

    assert rules.sources == ("AGENTS.override.md", "src/feature/AGENTS.md")
    assert "root override" in rules.text
    assert "nested agents" in rules.text
    assert "root agents" not in rules.text
    assert "nested claude" not in rules.text


def test_target_file_loads_only_its_ancestor_chain(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("root", encoding="utf-8")
    (tmp_path / "sibling").mkdir()
    (tmp_path / "sibling" / "AGENTS.md").write_text("sibling", encoding="utf-8")
    target = tmp_path / "src" / "mod.py"
    target.parent.mkdir()
    target.write_text("pass", encoding="utf-8")
    (target.parent / "CLAUDE.md").write_text("target policy", encoding="utf-8")

    rules = load_repository_instructions(Workspace(tmp_path), target)

    assert rules.sources == ("AGENTS.md", "src/CLAUDE.md")
    assert "sibling" not in rules.text


def test_utf8_bom_and_size_limits_are_reported(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(b"\xef\xbb\xbf" + ("界" * 9_000).encode("utf-8"))

    rules = load_repository_instructions(Workspace(tmp_path), tmp_path)

    assert rules.truncated
    assert rules.text.startswith("[source=AGENTS.md]\n")
    assert "\ufeff" not in rules.text
    assert len(rules.text) < 8_100
    assert rules.text.endswith("...[repository instructions truncated]")


def test_rules_ignored_by_gitignore_are_still_loaded(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("AGENTS.md\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("explicit rule", encoding="utf-8")

    rules = load_repository_instructions(Workspace(tmp_path), tmp_path)

    assert "explicit rule" in rules.text


def test_outside_target_does_not_load_ancestor_instructions(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("outside rule", encoding="utf-8")
    try:
        result = load_repository_instructions(Workspace(tmp_path), outside)
        assert result.text == ""
    finally:
        (outside / "AGENTS.md").unlink()
        outside.rmdir()


def test_symlink_escape_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("outside rule", encoding="utf-8")
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        (outside / "AGENTS.md").unlink()
        outside.rmdir()
        pytest.skip(f"directory symlinks unavailable: {error}")
    try:
        result = load_repository_instructions(Workspace(tmp_path), link)
        assert "outside rule" not in result.text
    finally:
        link.unlink()
        (outside / "AGENTS.md").unlink()
        outside.rmdir()


def test_read_and_list_tools_expose_scoped_rules_without_recording_them_as_target_read(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Nested editing policy", encoding="utf-8")
    target = nested / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path)

    read = ReadFileTool(workspace).run(ReadFileParameters(path="src/module.py"))
    listing = ListFilesTool(workspace).run(ListFilesParameters(path="src"))

    assert "Nested editing policy" in read.output
    assert read.metadata["instruction_sources"] == ["src/AGENTS.md"]
    assert workspace.read_version(nested / "AGENTS.md") is None
    assert "Nested editing policy" in listing.output
    assert listing.metadata["instruction_sources"] == ["src/AGENTS.md"]


def test_context_includes_root_instructions(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Root policy", encoding="utf-8")
    manager = ContextManager(Workspace(tmp_path))

    system_message, _ = manager.start(RunConfig(task="inspect", workspace=tmp_path))

    text = system_message.content[0].text  # type: ignore[union-attr]
    assert "Root policy" in text


def test_aggregate_instruction_limit_marks_omitted_directories(tmp_path: Path) -> None:
    current = tmp_path
    for depth in range(4):
        (current / "AGENTS.md").write_text(str(depth) * 8_000, encoding="utf-8")
        current = current / "nested"
        current.mkdir()
    rules = load_repository_instructions(Workspace(tmp_path), current)
    assert rules.truncated
    assert len(rules.sources) == 3
    assert "3333" not in rules.text


def test_copilot_hint_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-private.md"
    outside.write_text("PRIVATE_OUTSIDE_WORKSPACE", encoding="utf-8")
    target = tmp_path / ".github" / "copilot-instructions.md"
    target.parent.mkdir()
    try:
        target.symlink_to(outside)
    except OSError as error:
        outside.unlink()
        pytest.skip(f"symlinks unavailable: {error}")
    try:
        messages = ContextManager(Workspace(tmp_path)).start(
            RunConfig(task="inspect", workspace=tmp_path)
        )
        assert "PRIVATE_OUTSIDE_WORKSPACE" not in messages[0].model_dump_json()
    finally:
        target.unlink()
        outside.unlink()
