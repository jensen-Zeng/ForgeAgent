"""Append-only, fsync-before-acknowledge storage for RLM sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock, RLock
from uuid import UUID, uuid4

from pydantic import ValidationError

from forge_agent.session.models import SessionEntry, SessionHeader, TailInspection

_LOCKS_GUARD = Lock()
_PATH_LOCKS: dict[Path, RLock] = {}


class SessionCorruptError(RuntimeError):
    """A journal is invalid before its final, repairable tail."""


class SessionStore:
    """Own a version-1 UTF-8 JSONL session journal and its artifacts."""

    def __init__(self, directory: Path, header: SessionHeader) -> None:
        self.directory = directory.resolve(strict=False)
        self.path = self.directory / "session.jsonl"
        self.family_path = self.directory / "family.jsonl"
        self.artifacts = self.directory / "artifacts"
        self.header = header
        with _LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(self.path, RLock())

    @classmethod
    def create(
        cls,
        workspace: Path,
        *,
        session_id: UUID | None = None,
        root_session_id: UUID | None = None,
        parent_id: UUID | None = None,
        generation: int = 0,
        directory: Path | None = None,
    ) -> SessionStore:
        resolved_workspace = workspace.resolve(strict=False)
        sid = session_id or uuid4()
        root_id = root_session_id or sid
        target = directory or resolved_workspace / ".forge" / "sessions" / str(root_id)
        target.mkdir(parents=True, exist_ok=False)
        (target / "artifacts").mkdir()
        header = SessionHeader(
            session_id=sid,
            root_session_id=root_id,
            parent_id=parent_id,
            generation=generation,
            workspace=str(resolved_workspace),
        )
        store = cls(target, header)
        store._append_line(store.path, header.model_dump_json())
        if sid == root_id:
            store._append_line(store.family_path, header.model_dump_json())
        return store

    @classmethod
    def open(cls, workspace: Path, session_id: UUID | str) -> SessionStore:
        sid = UUID(str(session_id))
        directory = workspace.resolve(strict=False) / ".forge" / "sessions" / str(sid)
        path = directory / "session.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"session not found: {sid}")
        inspection = cls._inspect(path)
        if inspection.last_good_offset == 0:
            raise SessionCorruptError(inspection.error or "session header is unreadable")
        with path.open("r", encoding="utf-8", newline="") as stream:
            first = stream.readline()
        try:
            header = SessionHeader.model_validate_json(first)
        except ValidationError as error:
            raise SessionCorruptError("invalid session header") from error
        return cls(directory, header)

    def append(
        self,
        entry_type: str,
        payload: dict[str, object],
        *,
        parent_id: UUID | None = None,
        entry_id: UUID | None = None,
    ) -> SessionEntry:
        entry = SessionEntry(
            id=entry_id or uuid4(),
            parent_id=parent_id,
            type=entry_type,
            session_id=self.header.session_id,
            root_session_id=self.header.root_session_id,
            generation=self.header.generation,
            payload=payload,  # type: ignore[arg-type]
        )
        self._append_line(self.path, entry.model_dump_json())
        return entry

    def append_family(self, entry: SessionEntry) -> None:
        self._append_line(self.family_path, entry.model_dump_json())

    def replay(self) -> tuple[SessionEntry, ...]:
        inspection = self.inspect_tail()
        if inspection.error and inspection.last_good_offset < inspection.file_size:
            # A partial final line is explicitly recoverable and is simply omitted.
            pass
        entries: list[SessionEntry] = []
        with self.path.open("rb") as stream:
            header_line = stream.readline()
            offset = len(header_line)
            while offset < inspection.last_good_offset:
                raw = stream.readline()
                offset += len(raw)
                if not raw.strip():
                    continue
                try:
                    entries.append(SessionEntry.model_validate_json(raw))
                except ValidationError as error:
                    raise SessionCorruptError(f"invalid entry ending at byte {offset}") from error
        return tuple(entries)

    def inspect_tail(self) -> TailInspection:
        return self._inspect(self.path)

    def repair_tail(self, *, truncate_tail: bool = False) -> TailInspection:
        inspection = self.inspect_tail()
        if not truncate_tail or inspection.valid:
            return inspection
        with self._lock, self.path.open("r+b") as stream:
            stream.truncate(inspection.last_good_offset)
            stream.flush()
            os.fsync(stream.fileno())
        return self.inspect_tail()

    def allocate_artifact(self, suffix: str = ".txt") -> Path:
        self.artifacts.mkdir(parents=True, exist_ok=True)
        return self.artifacts / f"{uuid4()}{suffix}"

    def _append_line(self, path: Path, text: str) -> None:
        raw = (text + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _inspect(path: Path) -> TailInspection:
        raw = path.read_bytes()
        if not raw:
            return TailInspection(
                valid=False, last_good_offset=0, file_size=0, error="empty journal"
            )
        offset = 0
        last_good = 0
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            offset += len(line)
            if not line.endswith((b"\n", b"\r")):
                return TailInspection(
                    valid=False,
                    last_good_offset=last_good,
                    file_size=len(raw),
                    error="partial final JSONL line",
                )
            try:
                decoded = line.decode("utf-8")
                json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                return TailInspection(
                    valid=False,
                    last_good_offset=last_good,
                    file_size=len(raw),
                    error=f"invalid JSONL line {index + 1}: {type(error).__name__}",
                )
            last_good = offset
        return TailInspection(valid=True, last_good_offset=last_good, file_size=len(raw))
