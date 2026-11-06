"""Bounded, workspace-local repository instruction discovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forge_agent.tools.workspace import Workspace

_RULE_NAMES = ("AGENTS.override.md", "AGENTS.md", "CLAUDE.md")
_MAX_FILE_CHARS = 8_000
_MAX_TOTAL_CHARS = 24_000
_MAX_FILE_BYTES = 32 * 1024


@dataclass(frozen=True, slots=True)
class RepositoryInstructions:
    """Instructions and source labels for one target directory."""

    text: str
    sources: tuple[str, ...]
    truncated: bool = False


def load_repository_instructions(workspace: Workspace, target: Path) -> RepositoryInstructions:
    """Load one rule file per directory from workspace root through target parent.

    Rule files are explicit inputs and are not suppressed by `.gitignore`. They
    remain subject to workspace containment, symlink, sensitivity and size checks.
    """
    target_path = target if target.is_absolute() else workspace.root / target
    try:
        resolved_target = target_path.resolve(strict=False)
        resolved_target.relative_to(workspace.root)
    except (OSError, ValueError):
        return RepositoryInstructions(text="", sources=())

    target_directory = resolved_target if resolved_target.is_dir() else resolved_target.parent
    try:
        relative_directory = target_directory.relative_to(workspace.root)
    except ValueError:
        return RepositoryInstructions(text="", sources=())

    directories = [workspace.root]
    current = workspace.root
    for part in relative_directory.parts:
        current = current / part
        try:
            resolved = current.resolve(strict=True)
        except OSError:
            break
        if resolved != current or not workspace.is_within_root(resolved) or not resolved.is_dir():
            break
        directories.append(resolved)

    records: list[str] = []
    sources: list[str] = []
    total_chars = 0
    truncated = False
    for directory_index, directory in enumerate(directories):
        for name in _RULE_NAMES:
            path = directory / name
            try:
                if path.is_symlink() or not path.is_file() or not workspace.is_within_root(path):
                    continue
                if workspace.is_sensitive(path):
                    continue
                with path.open("rb") as stream:
                    raw = stream.read(_MAX_FILE_BYTES + 1)
                byte_truncated = len(raw) > _MAX_FILE_BYTES
                raw = raw[:_MAX_FILE_BYTES]
                try:
                    content = raw.decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    # A bounded read may end in the middle of a UTF-8 code point.
                    if byte_truncated and error.end == len(raw):
                        content = raw[: error.start].decode("utf-8-sig")
                    else:
                        continue
            except (OSError, UnicodeError):
                continue
            content_truncated = len(content) > _MAX_FILE_CHARS
            remaining = _MAX_TOTAL_CHARS - total_chars
            if remaining <= 0:
                truncated = True
                break
            available = min(_MAX_FILE_CHARS, remaining)
            if len(content) > available:
                content = content[:available]
                truncated = True
            relative_source = workspace.display_path(path)
            records.append(f"[source={relative_source}]\n{content}")
            sources.append(relative_source)
            total_chars += len(content)
            truncated = truncated or content_truncated or byte_truncated
            break
        if total_chars >= _MAX_TOTAL_CHARS:
            truncated = truncated or directory_index < len(directories) - 1
            break

    text = "\n\n".join(records)
    if truncated and text:
        text += "\n...[repository instructions truncated]"
    return RepositoryInstructions(text=text, sources=tuple(sources), truncated=truncated)
