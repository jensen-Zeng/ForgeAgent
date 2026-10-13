"""Exact-context, workspace-bounded, atomic patch application."""

from __future__ import annotations

import difflib
import hashlib
import os
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from forge_agent.locking import LockUnavailable, acquire_locks
from forge_agent.tools.base import BaseTool, ToolError, ToolExecution
from forge_agent.tools.workspace import Workspace, WorkspaceError
from forge_agent.tracing import EventKind, RunTracer

BeforeCommit = Callable[[], None]


class PatchEdit(BaseModel):
    """One exact replacement or new-file creation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    old_text: str | None = None
    new_text: str
    base_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_operation(self) -> PatchEdit:
        if self.old_text is None:
            if self.base_sha256 is not None:
                raise ValueError("new-file creation must not specify base_sha256")
            if not self.new_text:
                raise ValueError("new files cannot be empty")
        elif not self.old_text:
            raise ValueError("old_text cannot be empty for an existing file")
        elif self.old_text == self.new_text:
            raise ValueError("old_text and new_text must differ")
        return self


class ApplyPatchParameters(BaseModel):
    """Bounded collection of exact edits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    edits: tuple[PatchEdit, ...] = Field(min_length=1, max_length=20)


class PatchReview(BaseModel):
    """Reviewable patch evidence created before any file is changed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_files: tuple[str, ...]
    diff: str
    added_lines: int
    deleted_lines: int
    preexisting_changes: tuple[str, ...]
    workspace_status: tuple[str, ...]


PatchApprovalHandler = Callable[[PatchReview], bool]


@dataclass(slots=True)
class _PreparedEdit:
    path: Path
    display_path: str
    original_bytes: bytes | None
    new_bytes: bytes
    diff: str
    added_lines: int
    deleted_lines: int
    mode: int | None
    temp_path: Path | None = None


class ApplyPatchTool(BaseTool[ApplyPatchParameters]):
    """Apply exact patches without exposing arbitrary file overwrite."""

    name = "apply_patch"
    description = (
        "Create or modify text files using exact old_text/new_text replacements. "
        "Read existing files with read_file first. Existing context must match exactly once; "
        "on stale_read or concurrent_modification, re-read and review the change. "
        "Optional base_sha256 pins the read version. File deletion is not supported."
    )
    parameters_type: ClassVar[type[BaseModel]] = ApplyPatchParameters

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_files: int = 10,
        max_added_lines: int = 500,
        max_deleted_lines: int = 500,
        before_commit: BeforeCommit | None = None,
        approval_handler: PatchApprovalHandler | None = None,
        approval_mode: str = "never",
        tracer: RunTracer | None = None,
        require_read: bool = False,
        lock_timeout: float = 5.0,
    ) -> None:
        if approval_mode not in {"always", "never"}:
            raise ValueError("patch approval_mode must be 'always' or 'never'")
        self._workspace = workspace
        self._max_files = max_files
        self._max_added_lines = max_added_lines
        self._max_deleted_lines = max_deleted_lines
        self._before_commit = before_commit
        self._approval_handler = approval_handler
        self._approval_mode = approval_mode
        self._tracer = tracer
        self._require_read = require_read
        self._lock_timeout = lock_timeout

    def run(self, parameters: ApplyPatchParameters) -> ToolExecution:
        if len(parameters.edits) > self._max_files:
            raise ToolError(
                f"Patch touches {len(parameters.edits)} files; limit is {self._max_files}",
                code="patch_file_limit",
            )
        paths = [edit.path.casefold() for edit in parameters.edits]
        if len(paths) != len(set(paths)):
            raise ToolError(
                "A file may appear only once in one patch request",
                code="duplicate_patch_target",
            )

        prepared = [self._prepare(edit) for edit in parameters.edits]
        canonical = [os.path.normcase(str(edit.path)) for edit in prepared]
        if len(set(canonical)) != len(canonical):
            raise ToolError("Patch paths alias the same file", code="duplicate_patch_target")
        total_added = sum(edit.added_lines for edit in prepared)
        total_deleted = sum(edit.deleted_lines for edit in prepared)
        if total_added > self._max_added_lines:
            raise ToolError(
                f"Patch adds {total_added} lines; limit is {self._max_added_lines}",
                code="patch_added_line_limit",
            )
        if total_deleted > self._max_deleted_lines:
            raise ToolError(
                f"Patch deletes {total_deleted} lines; limit is {self._max_deleted_lines}",
                code="patch_deleted_line_limit",
            )

        preexisting = self._preexisting_changes(tuple(edit.display_path for edit in prepared))
        combined_diff = "\n".join(edit.diff for edit in prepared)
        changed_files = [edit.display_path for edit in prepared]
        workspace_status = self._git_status()
        review = PatchReview(
            changed_files=tuple(changed_files),
            diff=combined_diff,
            added_lines=total_added,
            deleted_lines=total_deleted,
            preexisting_changes=tuple(preexisting),
            workspace_status=tuple(workspace_status),
        )
        if not self._authorize(review):
            raise ToolError(
                "Patch rejected by local approval policy",
                code="approval_denied",
                metadata={"changed_files": cast(list[JsonValue], changed_files)},
            )
        try:
            with acquire_locks((edit.path for edit in prepared), timeout=self._lock_timeout):
                self._write_transaction(prepared)
        except LockUnavailable as error:
            raise ToolError(
                "Another writer holds the file lock; retry after re-reading the file",
                code="write_lock_timeout",
            ) from error
        for edit in prepared:
            self._workspace.record_read(edit.path, edit.new_bytes)
        return ToolExecution(
            output=combined_diff,
            metadata={
                "changed_files": cast(list[JsonValue], changed_files),
                "file_count": len(prepared),
                "added_lines": total_added,
                "deleted_lines": total_deleted,
                "preexisting_changes": cast(list[JsonValue], preexisting),
                "workspace_status_before": cast(list[JsonValue], workspace_status),
                "verified_after_write": True,
                "reverse_patch_available": bool(combined_diff),
            },
        )

    def _authorize(self, review: PatchReview) -> bool:
        if self._approval_mode == "never":
            return True
        self._emit_approval(EventKind.APPROVAL_REQUESTED, review, approved=None)
        approved = self._approval_handler is not None and self._approval_handler(review)
        self._emit_approval(EventKind.APPROVAL_RESOLVED, review, approved=approved)
        return approved

    def _emit_approval(
        self,
        kind: EventKind,
        review: PatchReview,
        *,
        approved: bool | None,
    ) -> None:
        if self._tracer is None:
            return
        self._tracer.emit(
            kind,
            data={
                "operation": "apply_patch",
                "changed_files": cast(list[JsonValue], list(review.changed_files)),
                "added_lines": review.added_lines,
                "deleted_lines": review.deleted_lines,
                "preexisting_changes": cast(
                    list[JsonValue],
                    list(review.preexisting_changes),
                ),
                "approved": approved,
            },
        )

    def _prepare(self, edit: PatchEdit) -> _PreparedEdit:
        path = self._resolve_target(edit)
        display = self._workspace.display_path(path)
        if edit.old_text is None:
            original_bytes = None
            original_text = ""
            newline = "\n"
            has_bom = False
            mode = None
        else:
            try:
                original_bytes = path.read_bytes()
            except OSError as error:
                raise ToolError(
                    f"Unable to read {display}: {error}",
                    code="patch_read_error",
                ) from error
            original_text, newline, has_bom = self._decode_text(
                original_bytes,
                display,
            )
            actual = hashlib.sha256(original_bytes).hexdigest()
            observed = self._workspace.read_version(path)
            if self._require_read and observed is None:
                raise ToolError(
                    f"Read {display} with read_file before modifying it", code="read_required"
                )
            if any(
                version is not None and version != actual
                for version in (observed, edit.base_sha256)
            ):
                raise ToolError(
                    f"{display} changed since it was read; re-read and review before retrying",
                    code="stale_read",
                    metadata={"path": display},
                )
            mode = path.stat().st_mode

        old_text = self._canonical_newlines(edit.old_text or "")
        new_text = self._canonical_newlines(edit.new_text)
        if edit.old_text is None:
            updated_text = new_text
        else:
            occurrences = original_text.count(old_text)
            if occurrences == 0:
                raise ToolError(
                    f"Patch context does not match current content in {display}",
                    code="patch_context_mismatch",
                )
            if occurrences > 1:
                raise ToolError(
                    f"Patch context is ambiguous in {display}; matched {occurrences} times",
                    code="patch_context_ambiguous",
                )
            updated_text = original_text.replace(old_text, new_text, 1)
            if not updated_text:
                raise ToolError(
                    f"Patch would empty {display}; file deletion is denied",
                    code="patch_delete_denied",
                )

        added, deleted = self._line_delta(original_text, updated_text)
        diff = "\n".join(
            difflib.unified_diff(
                original_text.splitlines(),
                updated_text.splitlines(),
                fromfile=f"a/{display}",
                tofile=f"b/{display}",
                lineterm="",
            )
        )
        rendered = updated_text.replace("\n", newline)
        encoded = rendered.encode("utf-8")
        if has_bom:
            encoded = b"\xef\xbb\xbf" + encoded
        return _PreparedEdit(
            path=path,
            display_path=display,
            original_bytes=original_bytes,
            new_bytes=encoded,
            diff=diff,
            added_lines=added,
            deleted_lines=deleted,
            mode=mode,
        )

    def _resolve_target(self, edit: PatchEdit) -> Path:
        raw = Path(edit.path).expanduser()
        candidate = raw if raw.is_absolute() else self._workspace.root / raw
        candidate = candidate.resolve(strict=False)
        if not self._workspace.is_within_root(candidate):
            raise ToolError(
                f"Patch target escapes workspace: {edit.path}",
                code="path_outside_workspace",
            )
        if self._workspace.is_sensitive(candidate):
            raise ToolError(
                f"Sensitive path is not writable: {edit.path}",
                code="sensitive_path",
            )
        if self._workspace.is_ignored(candidate):
            raise ToolError(
                f"Path is ignored by workspace policy: {edit.path}",
                code="path_ignored",
            )
        if edit.old_text is None:
            if candidate.exists():
                raise ToolError(
                    f"New-file patch target already exists: {edit.path}",
                    code="patch_target_exists",
                )
            try:
                parent = candidate.parent.resolve(strict=True)
            except OSError as error:
                raise ToolError(
                    f"Parent directory does not exist: {edit.path}",
                    code="patch_parent_missing",
                ) from error
            if not parent.is_dir() or not self._workspace.is_within_root(parent):
                raise ToolError(
                    f"Invalid patch parent: {edit.path}",
                    code="path_outside_workspace",
                )
            return candidate
        try:
            return self._workspace.resolve(candidate, require_file=True)
        except WorkspaceError as error:
            raise ToolError(str(error), code=error.code) from error

    def _write_transaction(self, prepared: list[_PreparedEdit]) -> None:
        try:
            for edit in prepared:
                edit.temp_path = self._write_temp(edit)
            if self._before_commit is not None:
                self._before_commit()
            for edit in prepared:
                self._verify_unchanged(edit)
            committed: list[_PreparedEdit] = []
            try:
                for edit in prepared:
                    if edit.temp_path is None:
                        raise AssertionError("temporary patch file was not prepared")
                    self._verify_unchanged(edit)
                    if edit.original_bytes is None:
                        # Linking is an atomic create-if-absent, never an overwrite.
                        os.link(edit.temp_path, edit.path)
                    else:
                        os.replace(edit.temp_path, edit.path)
                        edit.temp_path = None
                    committed.append(edit)
                    if edit.mode is not None:
                        edit.path.chmod(edit.mode)
            except (OSError, ToolError) as error:
                conflicts = self._rollback(committed)
                raise ToolError(
                    f"Atomic patch commit failed: {type(error).__name__}",
                    code=error.code if isinstance(error, ToolError) else "patch_write_error",
                    metadata={"rollback_conflicts": cast(list[JsonValue], conflicts)},
                ) from error
            for edit in prepared:
                try:
                    verified = (
                        edit.path.resolve() == edit.path
                        and edit.path.read_bytes() == edit.new_bytes
                    )
                except OSError:
                    verified = False
                if not verified:
                    conflicts = self._rollback(prepared)
                    raise ToolError(
                        f"Patch verification failed for {edit.display_path}",
                        code="patch_verification_error",
                        metadata={"rollback_conflicts": cast(list[JsonValue], conflicts)},
                    )
                self._invalidate_python_cache(edit.path)
        finally:
            for edit in prepared:
                if edit.temp_path is not None:
                    with suppress(OSError):
                        edit.temp_path.unlink(missing_ok=True)

    def _write_temp(self, edit: _PreparedEdit) -> Path:
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".forge-patch-",
                suffix=".tmp",
                dir=edit.path.parent,
                delete=False,
            ) as stream:
                stream.write(edit.new_bytes)
                stream.flush()
                os.fsync(stream.fileno())
                return Path(stream.name)
        except OSError as error:
            raise ToolError(
                f"Unable to prepare atomic write for {edit.display_path}",
                code="patch_write_error",
            ) from error

    def _verify_unchanged(self, edit: _PreparedEdit) -> None:
        if edit.path.resolve() != edit.path or not self._workspace.is_within_root(edit.path):
            raise ToolError("Patch path changed during update", code="concurrent_modification")
        if edit.original_bytes is None:
            if edit.path.exists():
                raise ToolError(
                    f"Patch target appeared during update: {edit.display_path}",
                    code="concurrent_modification",
                )
            return
        try:
            current = edit.path.read_bytes()
        except OSError as error:
            raise ToolError(
                f"Patch target changed during update: {edit.display_path}",
                code="concurrent_modification",
            ) from error
        if hashlib.sha256(current).digest() != hashlib.sha256(edit.original_bytes).digest():
            raise ToolError(
                f"Patch target changed during update: {edit.display_path}",
                code="concurrent_modification",
            )

    def _rollback(self, committed: list[_PreparedEdit]) -> list[str]:
        conflicts: list[str] = []
        for edit in reversed(committed):
            rollback_path: Path | None = None
            try:
                # A rollback must not undo a third party's newly saved bytes.
                if edit.path.resolve() != edit.path or edit.path.read_bytes() != edit.new_bytes:
                    conflicts.append(edit.display_path)
                    continue
                if edit.original_bytes is None:
                    edit.path.unlink(missing_ok=True)
                else:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        prefix=".forge-rollback-",
                        suffix=".tmp",
                        dir=edit.path.parent,
                        delete=False,
                    ) as stream:
                        stream.write(edit.original_bytes)
                        stream.flush()
                        os.fsync(stream.fileno())
                        rollback_path = Path(stream.name)
                    if edit.path.read_bytes() != edit.new_bytes:
                        conflicts.append(edit.display_path)
                        continue
                    os.replace(rollback_path, edit.path)
                    if edit.mode is not None:
                        edit.path.chmod(edit.mode)
            except OSError:
                conflicts.append(edit.display_path)
            finally:
                if rollback_path is not None:
                    with suppress(OSError):
                        rollback_path.unlink(missing_ok=True)
        return conflicts

    def _invalidate_python_cache(self, path: Path) -> None:
        if path.suffix.casefold() != ".py":
            return
        cache_directory = path.parent / "__pycache__"
        if not cache_directory.is_dir():
            return
        for cached in cache_directory.glob(f"{path.stem}.*.pyc"):
            with suppress(OSError):
                cached.unlink()

    def _preexisting_changes(self, paths: tuple[str, ...]) -> list[str]:
        command = [
            "git",
            "-C",
            str(self._workspace.root),
            "status",
            "--porcelain",
            "--",
            *paths,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        return [line[3:].strip() for line in completed.stdout.splitlines() if len(line) >= 4]

    def _git_status(self) -> list[str]:
        try:
            completed = subprocess.run(
                ["git", "-C", str(self._workspace.root), "status", "--short"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        return completed.stdout.splitlines()[:200]

    def _decode_text(
        self,
        raw: bytes,
        display: str,
    ) -> tuple[str, str, bool]:
        if b"\x00" in raw[:8192]:
            raise ToolError(
                f"Binary file cannot be patched: {display}",
                code="binary_file",
            )
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            decoded = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ToolError(
                f"File is not valid UTF-8 text: {display}",
                code="invalid_encoding",
            ) from error
        newline = "\r\n" if b"\r\n" in raw else "\n"
        return self._canonical_newlines(decoded), newline, has_bom

    def _canonical_newlines(self, text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _line_delta(self, old: str, new: str) -> tuple[int, int]:
        matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new.splitlines())
        added = 0
        deleted = 0
        for operation, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if operation in {"replace", "delete"}:
                deleted += old_end - old_start
            if operation in {"replace", "insert"}:
                added += new_end - new_start
        return added, deleted
