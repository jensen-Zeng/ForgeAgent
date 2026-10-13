"""Filesystem boundary, ignore rules, and text-file safety checks."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from pathspec import GitIgnoreSpec

_ALWAYS_IGNORED_DIRS: Final = frozenset(
    {
        ".git",
        ".forge",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
_SENSITIVE_NAMES: Final = frozenset(
    {
        ".env",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "netrc",
    }
)
_SENSITIVE_SUFFIXES: Final = frozenset({".key", ".p12", ".pfx", ".pem"})


class WorkspaceError(ValueError):
    """Expected workspace access failure with a stable error code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class Workspace:
    """Resolve all paths against one trusted and existing root directory."""

    def __init__(self, root: Path, *, respect_gitignore: bool = True) -> None:
        resolved = root.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"workspace root is not a directory: {resolved}")
        self.root = resolved
        self.respect_gitignore = respect_gitignore
        self._gitignore = self._load_root_gitignore()
        self._read_versions: dict[str, str] = {}

    def record_read(self, path: Path, raw: bytes) -> str:
        """Bind a successful tool read to its exact on-disk bytes for this registry."""
        digest = hashlib.sha256(raw).hexdigest()
        self._read_versions[os.path.normcase(str(path.resolve()))] = digest
        return digest

    def read_version(self, path: Path) -> str | None:
        """Return the last host-observed version; callers cannot forge this state."""
        return self._read_versions.get(os.path.normcase(str(path.resolve())))

    def resolve(
        self,
        raw_path: str | Path,
        *,
        require_file: bool = False,
        require_directory: bool = False,
        allow_sensitive: bool = False,
    ) -> Path:
        """Resolve a user/model path and enforce the workspace boundary."""
        path = Path(raw_path).expanduser()
        candidate = path if path.is_absolute() else self.root / path
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            unresolved = candidate.resolve(strict=False)
            self._require_within_root(unresolved)
            raise WorkspaceError(
                f"Path does not exist: {self.display_path(unresolved)}",
                code="path_not_found",
            ) from None
        except OSError as error:
            raise WorkspaceError(
                f"Unable to resolve path: {error}",
                code="path_resolution_error",
            ) from None
        self._require_within_root(resolved)
        if self.is_ignored(resolved):
            raise WorkspaceError(
                f"Path is ignored by workspace policy: {self.display_path(resolved)}",
                code="path_ignored",
            )
        if not allow_sensitive and self.is_sensitive(resolved):
            raise WorkspaceError(
                f"Sensitive path is not readable: {self.display_path(resolved)}",
                code="sensitive_path",
            )
        if require_file and not resolved.is_file():
            raise WorkspaceError(
                f"Path is not a file: {self.display_path(resolved)}",
                code="not_a_file",
            )
        if require_directory and not resolved.is_dir():
            raise WorkspaceError(
                f"Path is not a directory: {self.display_path(resolved)}",
                code="not_a_directory",
            )
        return resolved

    def display_path(self, path: Path) -> str:
        """Return a stable POSIX-style path relative to the workspace."""
        try:
            return path.relative_to(self.root).as_posix() or "."
        except ValueError:
            return str(path)

    def is_within_root(self, path: Path) -> bool:
        """Check containment after resolving links and normalizing case."""
        try:
            root = os.path.normcase(str(self.root))
            candidate = os.path.normcase(str(path.resolve(strict=False)))
            return os.path.commonpath((root, candidate)) == root
        except (OSError, ValueError):
            return False

    def is_ignored(self, path: Path) -> bool:
        """Apply fixed exclusions and the root .gitignore when configured."""
        if path == self.root:
            return False
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        if any(part in _ALWAYS_IGNORED_DIRS for part in relative.parts):
            return True
        if not self.respect_gitignore or self._gitignore is None:
            return False
        candidate = relative.as_posix()
        if path.is_dir():
            candidate += "/"
        return self._gitignore.match_file(candidate)

    def is_sensitive(self, path: Path) -> bool:
        """Recognize common credential and private-key file names."""
        name = path.name.casefold()
        if name == ".env" or name.startswith(".env."):
            return True
        return name in _SENSITIVE_NAMES or path.suffix.casefold() in _SENSITIVE_SUFFIXES

    def iter_files(self, start: Path) -> Iterator[Path]:
        """Yield safe regular files without following directory links."""
        if start.is_file():
            if not self.is_ignored(start) and not self.is_sensitive(start):
                yield start
            return
        for current_root, directory_names, file_names in os.walk(
            start,
            topdown=True,
            followlinks=False,
        ):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if self._is_safe_traversal_candidate(current / name, directory=True)
            )
            for name in sorted(file_names):
                candidate = current / name
                if self._is_safe_traversal_candidate(candidate, directory=False):
                    yield candidate

    def _is_safe_traversal_candidate(self, path: Path, *, directory: bool) -> bool:
        if path.is_symlink():
            return False
        if not self.is_within_root(path):
            return False
        if self.is_ignored(path) or self.is_sensitive(path):
            return False
        return path.is_dir() if directory else path.is_file()

    def _require_within_root(self, path: Path) -> None:
        if not self.is_within_root(path):
            raise WorkspaceError(
                f"Path escapes the workspace: {path}",
                code="path_outside_workspace",
            )

    def _load_root_gitignore(self) -> GitIgnoreSpec | None:
        if not self.respect_gitignore:
            return None
        gitignore_path = self.root / ".gitignore"
        if not gitignore_path.is_file():
            return None
        try:
            lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return None
        return GitIgnoreSpec.from_lines(lines)
