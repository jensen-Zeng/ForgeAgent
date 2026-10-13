"""Composition root for explicitly trusted first-stage RLM sessions."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from decimal import Decimal
from pathlib import Path
from threading import Lock
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from forge_agent.children import ChildManager
from forge_agent.compaction import CompactionPolicy, ContextBuilder
from forge_agent.config import ForgeSettings
from forge_agent.domain import (
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    Usage,
)
from forge_agent.engine import LoopAgentEngine, TurnResult
from forge_agent.kernel import HostBridge, KernelManager
from forge_agent.locking import acquire_locks
from forge_agent.messages import AgentMessageRouter
from forge_agent.models import ModelProvider, ProviderError
from forge_agent.session import InputEnvelope, SessionRuntime, SessionStore
from forge_agent.tools import ExecutePythonTool, ToolRegistry, Workspace

RegistryFactory = Callable[[], ToolRegistry]
_SECRET_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_SHELL_ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
)


class ShellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    executable: str = Field(min_length=1, max_length=500)
    args: tuple[str, ...] = Field(default=(), max_length=100)
    cwd: str = "."
    timeout_seconds: float = Field(default=120, gt=0, le=900)


class SpawnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    task: str = Field(min_length=1, max_length=1_000_000)


class ChildIdRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    child_id: UUID


class MessageSendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: UUID
    text: str = Field(min_length=1, max_length=32_000)


class RLMApplication:
    """Own the root runtime, trusted kernels, children, mailbox, and shutdown."""

    def __init__(
        self,
        *,
        settings: ForgeSettings,
        provider: ModelProvider,
        store: SessionStore,
        registry_factory: RegistryFactory,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.store = store
        self.registry_factory = registry_factory
        self._ownership_context = acquire_locks([store.directory / "runtime-owner"], timeout=0)
        self._ownership_held = False
        self._shutdown_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._closed = False
        self._shutdown_complete = False
        self._ownership_context.__enter__()
        self._ownership_held = True
        try:
            self.stores: dict[UUID, SessionStore] = {store.header.session_id: store}
            self.runtimes: dict[UUID, SessionRuntime] = {}
            self.kernels: dict[UUID, KernelManager] = {}
            self.bridges: dict[UUID, HostBridge] = {}
            self.usage_ledger = UsageLedger(store)
            self.root_runtime = self._make_runtime(store)
            self.child_manager = ChildManager(
                parent_store=store,
                root_store=store,
                runtime_factory=self._make_runtime,
                max_depth=settings.rlm.max_depth,
                max_concurrent=settings.rlm.max_concurrent_children,
                overflow=settings.rlm.child_overflow,
            )
            # The root bridge was created before ChildManager; handlers resolve it lazily.
            for session_store in tuple(self.stores.values()):
                self._register_bridge_handlers(session_store)
        except BaseException:
            self._release_ownership()
            raise

    async def submit_and_run(self, task: str) -> tuple[TurnResult, ...]:
        self._ensure_open()
        async with self._operation_lock:
            self._ensure_open()
            await self.root_runtime.submit(InputEnvelope(text=task))
            self._ensure_open()
            return await self.root_runtime.run_until_idle()

    async def resume(self) -> tuple[TurnResult, ...]:
        self._ensure_open()
        async with self._operation_lock:
            self._ensure_open()
            return await self.root_runtime.run_until_idle()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            if not self._closed:
                self._closed = True
                self.root_runtime.cancel("session_shutdown")
            # Keep session ownership until any operation holding the root
            # runtime has unwound and stopped appending to its journal.
            async with self._operation_lock:
                pass
            errors: list[BaseException] = []
            try:
                try:
                    await self.child_manager.cancel_all()
                except BaseException as error:
                    errors.append(error)
                for kernel in tuple(self.kernels.values()):
                    try:
                        await kernel.shutdown()
                    except BaseException as error:
                        errors.append(error)
                try:
                    await self.provider.aclose()
                except BaseException as error:
                    errors.append(error)
            finally:
                self._release_ownership()
                self._shutdown_complete = True
            if errors:
                raise errors[0]

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("session_closed")

    def _release_ownership(self) -> None:
        if self._ownership_held:
            context = self._ownership_context
            self._ownership_held = False
            context.__exit__(None, None, None)

    def _make_runtime(self, store: SessionStore) -> SessionRuntime:
        def persist_kernel_event(kind: str, data: dict[str, object]) -> None:
            store.append("runtime_event", {"kind": kind, "data": data})

        bridge = HostBridge(
            store.directory / ".bridge",
            timeout_seconds=float(self.settings.rlm.host_request_timeout_seconds),
        )
        kernel = KernelManager(
            workspace=Path(store.header.workspace),
            session_id=store.header.session_id,
            root_session_id=store.header.root_session_id,
            artifacts=store.artifacts,
            host_bridge=bridge,
            preview_bytes=self.settings.rlm.kernel_preview_bytes,
            event_sink=persist_kernel_event,
        )
        registry = self.registry_factory()
        registry.register(ExecutePythonTool(kernel))
        session_provider = BudgetedSessionProvider(
            self.provider,
            ledger=self.usage_ledger,
            session_id=store.header.session_id,
            max_tokens=self.settings.agent.max_tokens,
            max_cost=self.settings.agent.max_cost_usd,
        )
        builder = ContextBuilder(
            store=store,
            provider=session_provider,
            policy=CompactionPolicy(
                enabled=self.settings.context.compaction_enabled,
                window_tokens=self.settings.context.window_tokens
                or self.settings.context.max_tokens,
                reserve_tokens=self.settings.context.reserve_tokens,
                keep_recent_tokens=self.settings.context.keep_recent_tokens,
                max_tool_result_chars=self.settings.context.max_tool_result_chars,
            ),
        )
        config = self.settings.to_run_config("RLM session")
        runtime = SessionRuntime(
            store=store,
            engine=LoopAgentEngine(session_provider),
            tools=registry,
            base_config=config,
            context_builder=builder,
        )
        self.stores[store.header.session_id] = store
        self.runtimes[store.header.session_id] = runtime
        self.kernels[store.header.session_id] = kernel
        self.bridges[store.header.session_id] = bridge
        if hasattr(self, "child_manager"):
            self._register_bridge_handlers(store)
        return runtime

    def _register_bridge_handlers(self, sender: SessionStore) -> None:
        bridge = self.bridges[sender.header.session_id]
        if getattr(bridge, "_handlers", None):
            return

        async def spawn(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            if sender.header.generation >= self.settings.rlm.max_depth:
                raise ValueError("child_depth_exceeded")
            request = SpawnRequest.model_validate(payload)
            handle = self.child_manager.spawn(request.name, request.task)
            return {"child_id": str(handle.id), "state": handle.info.state.value}

        async def list_children(_: dict[str, JsonValue]) -> dict[str, JsonValue]:
            return {
                "children": [child.model_dump(mode="json") for child in self.child_manager.list()]
            }

        async def cancel(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            request = ChildIdRequest.model_validate(payload)
            self.child_manager.cancel(request.child_id)
            return {"child_id": str(request.child_id), "accepted": True}

        async def delete(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            request = ChildIdRequest.model_validate(payload)
            info = self.child_manager.delete(request.child_id)
            return {"child_id": str(info.id), "state": info.state.value}

        async def shell(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            return await self._run_shell(sender, ShellRequest.model_validate(payload))

        async def send_message(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
            request = MessageSendRequest.model_validate(payload)
            if request.target != self.store.header.session_id:
                child = self.child_manager.get(request.target)
                if child.state.value == "deleted":
                    raise ValueError("message_target_deleted")
            router = self._router(sender)
            message = router.send(request.target, request.text)
            if request.target in self.runtimes:
                await router.deliver_pending(request.target)
            return {"message_id": str(message.id), "accepted": True}

        async def list_messages(_: dict[str, JsonValue]) -> dict[str, JsonValue]:
            pending = self._router(sender).list_pending(sender.header.session_id)
            return {"messages": [message.model_dump(mode="json") for message in pending]}

        bridge.register("rlm.spawn", spawn)
        bridge.register("rlm.list", list_children)
        bridge.register("rlm.cancel", cancel)
        bridge.register("rlm.delete", delete)
        bridge.register("rlm.shell", shell)
        bridge.register("agent_message.send", send_message)
        bridge.register("agent_message.list", list_messages)

    def _router(self, sender: SessionStore) -> AgentMessageRouter:
        return AgentMessageRouter(
            sender_store=sender,
            stores=self.stores,
            runtimes=self.runtimes,
            per_minute=self.settings.session.messages_per_minute,
            max_pending=self.settings.session.max_pending_messages,
            max_total=self.settings.session.max_family_messages,
        )

    async def _run_shell(self, sender: SessionStore, request: ShellRequest) -> dict[str, JsonValue]:
        workspace = Workspace(Path(sender.header.workspace))
        cwd = workspace.resolve(request.cwd, require_directory=True)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SHELL_ENV_ALLOWLIST
            and not any(marker in key.upper() for marker in _SECRET_MARKERS)
        }
        environment.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        process = await asyncio.create_subprocess_exec(
            request.executable,
            *request.args,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            async with asyncio.timeout(request.timeout_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            timed_out = True
            await _kill_async_process(process)
            stdout, stderr = await process.communicate()
        combined = stdout + stderr
        artifact: str | None = None
        truncated = len(combined) > 65_536
        if truncated:
            path = sender.allocate_artifact(".log")
            path.write_bytes(combined)
            artifact = str(path)
            combined = combined[:65_536]
        return {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "output": combined.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "artifact": artifact,
        }


async def _kill_async_process(process: asyncio.subprocess.Process) -> None:
    try:
        import psutil

        parent = psutil.Process(process.pid)
        processes = (*parent.children(recursive=True), parent)
        for item in processes:
            with suppress(psutil.Error):
                item.terminate()
        _, alive = await asyncio.to_thread(psutil.wait_procs, processes, 2)
        for item in alive:
            with suppress(psutil.Error):
                item.kill()
    except Exception:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=2)


class SharedBudgetExceeded(ProviderError):
    """The root family has exhausted its shared token or cost budget."""


class UsageLedger:
    """Thread-safe root-family Usage accounting persisted after every response."""

    def __init__(self, root_store: SessionStore) -> None:
        self.root_store = root_store
        self.total_tokens = 0
        self.known_cost = Decimal("0")
        self.cost_complete = True
        self._lock = Lock()
        for entry in root_store.replay():
            if entry.type != "usage":
                continue
            total = entry.payload.get("family_total_tokens")
            cost = entry.payload.get("family_known_cost")
            complete = entry.payload.get("family_cost_complete")
            if isinstance(total, int):
                self.total_tokens = total
            if isinstance(cost, str):
                self.known_cost = Decimal(cost)
            if isinstance(complete, bool):
                self.cost_complete = complete

    def check(self, *, max_tokens: int | None, max_cost: Decimal | None) -> None:
        with self._lock:
            if max_tokens is not None and self.total_tokens >= max_tokens:
                raise SharedBudgetExceeded("root family token budget exhausted")
            if max_cost is not None and self.known_cost >= max_cost:
                raise SharedBudgetExceeded("root family cost budget exhausted")

    def record(self, session_id: UUID, usage: Usage) -> None:
        with self._lock:
            self.total_tokens += usage.total_tokens or 0
            if usage.cost is None:
                self.cost_complete = False
            else:
                self.known_cost += usage.cost
            self.root_store.append(
                "usage",
                {
                    "session_id": str(session_id),
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                    "cost": str(usage.cost) if usage.cost is not None else None,
                    "family_total_tokens": self.total_tokens,
                    "family_known_cost": str(self.known_cost),
                    "family_cost_complete": self.cost_complete,
                },
            )


class BudgetedSessionProvider:
    """Bind shared provider calls to one session and one root-family ledger."""

    def __init__(
        self,
        provider: ModelProvider,
        *,
        ledger: UsageLedger,
        session_id: UUID,
        max_tokens: int | None,
        max_cost: Decimal | None,
    ) -> None:
        self.provider = provider
        self.ledger = ledger
        self.session_id = session_id
        self.max_tokens = max_tokens
        self.max_cost = max_cost

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.ledger.check(max_tokens=self.max_tokens, max_cost=self.max_cost)
        response = await self.provider.generate(request)
        self.ledger.record(self.session_id, response.usage)
        return response

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        self.ledger.check(max_tokens=self.max_tokens, max_cost=self.max_cost)
        stream = getattr(self.provider, "stream", None)
        if stream is None:
            response = await self.provider.generate(request)
            self.ledger.record(self.session_id, response.usage)
            yield ModelStreamEvent(type=ModelStreamEventType.COMPLETED, response=response)
            return
        async for event in stream(request):
            if event.type is ModelStreamEventType.COMPLETED and event.response is not None:
                self.ledger.record(self.session_id, event.response.usage)
            yield event

    async def aclose(self) -> None:
        """The application, not an individual child, owns the shared provider."""
