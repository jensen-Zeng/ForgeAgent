"""Recursive in-process child agents with durable family state transitions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from forge_agent.session import InputEnvelope, SessionEntry, SessionRuntime, SessionStore

RuntimeFactory = Callable[[SessionStore], SessionRuntime]


class ChildState(StrEnum):
    ADMITTED = "admitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETED = "deleted"


class ChildInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    parent_id: UUID
    root_session_id: UUID
    name: str
    task: str
    generation: int = Field(ge=1)
    state: ChildState
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    final_text: str | None = None


class ChildHandle:
    """Stable non-blocking handle returned immediately after admission."""

    def __init__(self, manager: ChildManager, child_id: UUID) -> None:
        self._manager = manager
        self.id = child_id

    @property
    def info(self) -> ChildInfo:
        return self._manager.get(self.id)

    async def wait(self) -> ChildInfo:
        await self._manager.wait(self.id)
        return self.info

    def cancel(self) -> None:
        self._manager.cancel(self.id)


class ChildManager:
    """Manage children of one session while persisting to the root family log."""

    def __init__(
        self,
        *,
        parent_store: SessionStore,
        root_store: SessionStore,
        runtime_factory: RuntimeFactory,
        max_depth: int = 1,
        max_concurrent: int = 4,
        overflow: str = "reject",
    ) -> None:
        self.parent_store = parent_store
        self.root_store = root_store
        self.runtime_factory = runtime_factory
        self.max_depth = max_depth
        self.max_concurrent = max_concurrent
        self.overflow = overflow
        self._children: dict[UUID, ChildInfo] = {}
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._runtimes: dict[UUID, SessionRuntime] = {}
        self._names: set[str] = set()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._restore_history()

    def spawn(self, name: str, task: str) -> ChildHandle:
        if self.parent_store.header.generation >= self.max_depth:
            raise ValueError("child_depth_exceeded")
        normalized = name.strip()
        if not normalized:
            raise ValueError("child name cannot be blank")
        if normalized in self._names:
            raise ValueError("duplicate_child_name")
        active = sum(
            child.state in {ChildState.ADMITTED, ChildState.QUEUED, ChildState.RUNNING}
            for child in self._children.values()
        )
        if active >= self.max_concurrent and self.overflow == "reject":
            raise ValueError("child_concurrency_exceeded")
        now = datetime.now(UTC)
        child_id = uuid4()
        info = ChildInfo(
            id=child_id,
            parent_id=self.parent_store.header.session_id,
            root_session_id=self.root_store.header.root_session_id,
            name=normalized,
            task=task,
            generation=self.parent_store.header.generation + 1,
            state=ChildState.ADMITTED,
            created_at=now,
            updated_at=now,
        )
        self._children[child_id] = info
        self._names.add(normalized)
        self._persist(info)
        self._tasks[child_id] = asyncio.create_task(self._run(child_id))
        return ChildHandle(self, child_id)

    def list(self) -> tuple[ChildInfo, ...]:
        return tuple(sorted(self._children.values(), key=lambda child: child.created_at))

    def get(self, child_id: UUID) -> ChildInfo:
        try:
            return self._children[child_id]
        except KeyError as error:
            raise KeyError("child_not_found") from error

    async def wait(self, child_id: UUID) -> None:
        task = self._tasks.get(child_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def cancel(self, child_id: UUID) -> None:
        info = self.get(child_id)
        if info.state not in {ChildState.ADMITTED, ChildState.QUEUED, ChildState.RUNNING}:
            return
        runtime = self._runtimes.get(child_id)
        if runtime is not None:
            runtime.cancel("parent_cancelled")
        self._transition(child_id, ChildState.CANCELLED)
        task = self._tasks.get(child_id)
        if task is not None:
            task.cancel()

    async def cancel_all(self, *, timeout_seconds: float = 10.0) -> None:
        active = [
            child.id
            for child in self._children.values()
            if child.state in {ChildState.ADMITTED, ChildState.QUEUED, ChildState.RUNNING}
        ]
        for child_id in active:
            self.cancel(child_id)
        tasks = [self._tasks[child_id] for child_id in active if child_id in self._tasks]
        if tasks:
            try:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                for task in tasks:
                    task.cancel()

    def delete(self, child_id: UUID) -> ChildInfo:
        info = self.get(child_id)
        if info.state in {ChildState.ADMITTED, ChildState.QUEUED, ChildState.RUNNING}:
            raise ValueError("child_is_active")
        return self._transition(child_id, ChildState.DELETED)

    async def _run(self, child_id: UUID) -> None:
        info = self.get(child_id)
        self._transition(child_id, ChildState.QUEUED)
        try:
            async with self._semaphore:
                self._transition(child_id, ChildState.RUNNING)
                directory = self.root_store.directory / "children" / str(child_id)
                store = SessionStore.create(
                    Path(self.parent_store.header.workspace),
                    session_id=child_id,
                    root_session_id=self.root_store.header.root_session_id,
                    parent_id=info.parent_id,
                    generation=info.generation,
                    directory=directory,
                )
                runtime = self.runtime_factory(store)
                self._runtimes[child_id] = runtime
                await runtime.submit(InputEnvelope(text=info.task))
                results = await runtime.run_until_idle()
                if results and results[-1].status.value == "completed":
                    self._transition(
                        child_id,
                        ChildState.COMPLETED,
                        final_text=results[-1].final_text,
                    )
                else:
                    self._transition(child_id, ChildState.FAILED, error="child turn failed")
        except asyncio.CancelledError:
            if self.get(child_id).state is not ChildState.CANCELLED:
                self._transition(child_id, ChildState.CANCELLED)
        except Exception as error:
            self._transition(child_id, ChildState.FAILED, error=type(error).__name__)
        finally:
            self._runtimes.pop(child_id, None)

    def _transition(
        self,
        child_id: UUID,
        state: ChildState,
        *,
        error: str | None = None,
        final_text: str | None = None,
    ) -> ChildInfo:
        previous = self.get(child_id)
        info = previous.model_copy(
            update={
                "state": state,
                "updated_at": datetime.now(UTC),
                "error": error,
                "final_text": final_text or previous.final_text,
            }
        )
        self._children[child_id] = info
        self._persist(info)
        return info

    def _persist(self, info: ChildInfo) -> None:
        entry = SessionEntry(
            session_id=info.id,
            root_session_id=info.root_session_id,
            parent_id=info.parent_id,
            generation=info.generation,
            type="child_state",
            payload=info.model_dump(mode="json"),
        )
        self.root_store.append_family(entry)

    def _restore_history(self) -> None:
        if not self.root_store.family_path.exists():
            return
        lines = self.root_store.family_path.read_text(encoding="utf-8").splitlines()[1:]
        for line in lines:
            try:
                entry = SessionEntry.model_validate_json(line)
                if entry.type != "child_state":
                    continue
                info = ChildInfo.model_validate(entry.payload)
            except ValueError:
                continue
            if info.parent_id == self.parent_store.header.session_id:
                self._children[info.id] = info
                self._names.add(info.name)
        interrupted = [
            child.id
            for child in self._children.values()
            if child.state in {ChildState.ADMITTED, ChildState.QUEUED, ChildState.RUNNING}
        ]
        for child_id in interrupted:
            self._transition(child_id, ChildState.FAILED, error="interrupted_uncertain")
