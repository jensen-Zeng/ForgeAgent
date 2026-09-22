"""Lazy, persistent, strictly serialized IPython kernel management."""
# ruff: noqa: E501

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from forge_agent.kernel.bridge import HostBridge, HostRequest, HostResponse

_SECRET_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_ENV_ALLOWLIST = frozenset(
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
KernelEventSink = Callable[[str, dict[str, object]], None]


class KernelUnavailableError(RuntimeError):
    """The optional RLM kernel dependencies are not installed."""


class KernelExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    msg_id: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
    result: str | None = None
    traceback: tuple[str, ...] = ()
    timed_out: bool = False
    truncated: bool = False
    artifact: str | None = None
    restarted: bool = False


class KernelManager:
    """One trusted user-permission IPython kernel per RLM session."""

    def __init__(
        self,
        *,
        workspace: Path,
        session_id: UUID,
        root_session_id: UUID,
        artifacts: Path,
        host_bridge: HostBridge | None = None,
        preview_bytes: int = 65_536,
        environ: Mapping[str, str] | None = None,
        event_sink: KernelEventSink | None = None,
    ) -> None:
        self.workspace = workspace.resolve(strict=False)
        self.session_id = session_id
        self.root_session_id = root_session_id
        self.artifacts = artifacts
        self.preview_bytes = preview_bytes
        self.host_bridge = host_bridge
        self._environ = dict(environ or os.environ)
        self._event_sink = event_sink
        self._manager: Any = None
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._bootstrapped = False

    @property
    def started(self) -> bool:
        return self._manager is not None

    async def start(self) -> None:
        if self.started:
            return
        try:
            from jupyter_client.kernelspec import KernelSpecManager
            from jupyter_client.manager import AsyncKernelManager
        except ImportError as error:
            raise KernelUnavailableError(
                "RLM dependencies are missing; install ForgeAgent with .[rlm]"
            ) from error
        kernel_dirs = [str(Path(sys.prefix) / "share" / "jupyter" / "kernels")]
        spec_manager = KernelSpecManager(kernel_dirs=kernel_dirs)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        manager = AsyncKernelManager(
            kernel_name="python3",
            kernel_spec_manager=spec_manager,
            connection_file=str(self.artifacts / f"kernel-{self.session_id}.json"),
        )
        await manager.start_kernel(cwd=str(self.workspace), env=self._sanitized_environment())
        client = manager.client()
        client.start_channels()
        try:
            await client.wait_for_ready(timeout=10)
        except Exception:
            await manager.shutdown_kernel(now=True)
            raise
        self._manager = manager
        self._client = client
        self._emit("kernel_started", {"session_id": str(self.session_id)})
        await self._bootstrap()

    async def execute(
        self,
        code: str,
        *,
        timeout_seconds: float = 120.0,
    ) -> KernelExecution:
        async with self._lock:
            await self.start()
            return await self._execute_locked(code, timeout_seconds=timeout_seconds)

    async def interrupt(self) -> None:
        if self._manager is None:
            return
        await self._manager.interrupt_kernel()
        self._emit("kernel_interrupted", {"session_id": str(self.session_id)})

    async def restart(self) -> None:
        async with self._lock:
            await self._restart_locked()

    async def shutdown(self) -> None:
        async with self._lock:
            if self._client is not None:
                self._client.stop_channels()
            if self._manager is not None:
                with suppress(Exception):
                    await self._manager.shutdown_kernel(now=True)
            self._client = None
            self._manager = None
            self._bootstrapped = False
            if self.host_bridge is not None:
                await self.host_bridge.close()

    async def _execute_locked(
        self,
        code: str,
        *,
        timeout_seconds: float,
        allow_restart: bool = True,
    ) -> KernelExecution:
        msg_id = str(self._client.execute(code, allow_stdin=False, stop_on_error=False))
        self._emit(
            "kernel_execution_started",
            {"session_id": str(self.session_id), "msg_id": msg_id},
        )
        stdout: list[str] = []
        stderr: list[str] = []
        results: list[str] = []
        traceback: tuple[str, ...] = ()
        ok = True
        timed_out = False
        restarted = False
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    message = await self._client.get_iopub_msg(timeout=1)
                    parent = message.get("parent_header", {}).get("msg_id")
                    if parent != msg_id:
                        continue
                    message_type = message.get("msg_type")
                    content = message.get("content", {})
                    if message_type == "comm_open":
                        await self._handle_comm_open(content)
                    elif message_type == "stream":
                        target = stderr if content.get("name") == "stderr" else stdout
                        target.append(str(content.get("text", "")))
                    elif message_type in {"execute_result", "display_data"}:
                        data = content.get("data", {})
                        if isinstance(data, dict) and "text/plain" in data:
                            results.append(str(data["text/plain"]))
                    elif message_type == "error":
                        ok = False
                        raw_traceback = content.get("traceback", [])
                        if isinstance(raw_traceback, list):
                            traceback = tuple(str(line) for line in raw_traceback)
                    elif message_type == "status" and content.get("execution_state") == "idle":
                        break
        except TimeoutError:
            ok = False
            timed_out = True
            alive = False
            with suppress(Exception):
                alive = bool(await self._manager.is_alive())
            if alive:
                with suppress(Exception):
                    await self.interrupt()
                recovered = await self._wait_for_idle(msg_id, timeout_seconds=10)
            else:
                recovered = False
            if not recovered and allow_restart:
                await self._restart_locked()
                restarted = True
        except Exception:
            ok = False
            if allow_restart:
                await self._restart_locked()
                restarted = True

        full = "".join(stdout) + "".join(stderr) + "\n".join(results) + "\n".join(traceback)
        preview, truncated, artifact = self._bound_output(full)
        # Keep stream channels distinct when small; large data is represented by one preview.
        if truncated:
            stdout_text = preview
            stderr_text = ""
            result_text = None
            traceback = ()
        else:
            stdout_text = "".join(stdout)
            stderr_text = "".join(stderr)
            result_text = "\n".join(results) or None
        execution = KernelExecution(
            msg_id=msg_id,
            ok=ok,
            stdout=stdout_text,
            stderr=stderr_text,
            result=result_text,
            traceback=traceback,
            timed_out=timed_out,
            truncated=truncated,
            artifact=str(artifact) if artifact else None,
            restarted=restarted,
        )
        self._emit(
            "kernel_execution_completed",
            {
                "session_id": str(self.session_id),
                "msg_id": msg_id,
                "ok": execution.ok,
                "timed_out": execution.timed_out,
                "restarted": execution.restarted,
            },
        )
        return execution

    async def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        bridge_dir = str(self.host_bridge.directory) if self.host_bridge else ""
        code = _bootstrap_source(
            bridge_dir=bridge_dir,
            session_id=str(self.session_id),
            root_session_id=str(self.root_session_id),
            workspace=str(self.workspace),
        )
        result = await self._execute_locked(code, timeout_seconds=10, allow_restart=False)
        if not result.ok:
            raise RuntimeError("unable to initialize trusted RLM kernel bridge")
        self._bootstrapped = True

    async def _restart_locked(self) -> None:
        if self._manager is None:
            return
        try:
            self._emit("kernel_restarted", {"session_id": str(self.session_id)})
            await self._manager.restart_kernel(now=True)
            self._client.stop_channels()
            self._client = self._manager.client()
            self._client.start_channels()
            await self._client.wait_for_ready(timeout=10)
            self._bootstrapped = False
            await self._bootstrap()
        except Exception:
            with suppress(Exception):
                await self._manager.shutdown_kernel(now=True)
            self._manager = None
            self._client = None
            self._bootstrapped = False
            await self.start()

    async def _wait_for_idle(self, msg_id: str, *, timeout_seconds: float) -> bool:
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    message = await self._client.get_iopub_msg(timeout=1)
                    if message.get("parent_header", {}).get("msg_id") != msg_id:
                        continue
                    if (
                        message.get("msg_type") == "status"
                        and message.get("content", {}).get("execution_state") == "idle"
                    ):
                        return True
        except (TimeoutError, Exception):
            return False

    def _bound_output(self, text: str) -> tuple[str, bool, Path | None]:
        raw = text.encode("utf-8", errors="replace")
        if len(raw) <= self.preview_bytes:
            return text, False, None
        self.artifacts.mkdir(parents=True, exist_ok=True)
        path = (
            self.artifacts / f"kernel-{self.session_id}-{len(list(self.artifacts.iterdir()))}.txt"
        )
        path.write_bytes(raw)
        preview = raw[: self.preview_bytes].decode("utf-8", errors="ignore")
        preview += f"\n...[truncated; full output: {path.name}]"
        return preview, True, path

    async def _handle_comm_open(self, content: object) -> None:
        if self.host_bridge is None or not isinstance(content, dict):
            return
        if content.get("target_name") != "forge_host":
            return
        raw = content.get("data")
        if not isinstance(raw, dict):
            return
        try:
            request = HostRequest.model_validate(raw)
        except ValidationError:
            raw_id = raw.get("id")
            try:
                request_id = UUID(str(raw_id))
            except ValueError:
                return
            response = HostResponse(
                id=request_id,
                ok=False,
                error_code="invalid_host_request",
                error="host request schema rejected",
            )
        else:
            response = await self.host_bridge.dispatch(request)
        message = self._client.session.msg(
            "forge_host_reply",
            {
                "request_id": str(response.id),
                "response": response.model_dump(mode="json"),
            },
        )
        self._client.control_channel.send(message)

    def _sanitized_environment(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in self._environ.items():
            upper = key.upper()
            if upper not in _ENV_ALLOWLIST:
                continue
            if any(marker in upper for marker in _SECRET_MARKERS):
                continue
            result[key] = value
        result["PYTHONUTF8"] = "1"
        result["PYTHONIOENCODING"] = "utf-8"
        private_home = self.artifacts / ".runtime"
        ipython_dir = private_home / "ipython"
        jupyter_config = private_home / "jupyter-config"
        jupyter_data = private_home / "jupyter-data"
        jupyter_runtime = private_home / "jupyter-runtime"
        for directory in (ipython_dir, jupyter_config, jupyter_data, jupyter_runtime):
            directory.mkdir(parents=True, exist_ok=True)
        result["IPYTHONDIR"] = str(ipython_dir)
        result["JUPYTER_CONFIG_DIR"] = str(jupyter_config)
        result["JUPYTER_DATA_DIR"] = str(jupyter_data)
        result["JUPYTER_RUNTIME_DIR"] = str(jupyter_runtime)
        return result

    def _emit(self, kind: str, data: dict[str, object]) -> None:
        if self._event_sink is not None:
            self._event_sink(kind, data)


def _bootstrap_source(
    *, bridge_dir: str, session_id: str, root_session_id: str, workspace: str
) -> str:
    """Return dependency-free proxy code; no credentials or host objects are injected."""
    return textwrap.dedent(
        f"""
        import shlex as _forge_shlex
        import time as _forge_time
        import uuid as _forge_uuid
        from comm import create_comm as _forge_create_comm
        from types import MappingProxyType as _ForgeMappingProxyType

        _forge_bridge_enabled = {bool(bridge_dir)!r}
        _forge_responses = {{}}
        _forge_comms = []

        def _forge_reply_handler(stream, identities, parent):
            content = parent.get("content", {{}})
            request_id = content.get("request_id")
            response = content.get("response")
            if isinstance(request_id, str) and isinstance(response, dict):
                _forge_responses[request_id] = response

        get_ipython().kernel.control_handlers["forge_host_reply"] = _forge_reply_handler

        def _forge_request(operation, payload):
            if not _forge_bridge_enabled:
                raise RuntimeError("host bridge is unavailable")
            request_id = str(_forge_uuid.uuid4())
            request = {{"id": request_id, "operation": operation, "payload": payload}}
            comm = _forge_create_comm(target_name="forge_host", data=request)
            _forge_comms.append(comm)
            deadline = _forge_time.monotonic() + 10.0
            while _forge_time.monotonic() < deadline:
                response = _forge_responses.pop(request_id, None)
                if response is not None:
                    comm.close()
                    _forge_comms.remove(comm)
                    if not response["ok"]:
                        raise RuntimeError(response.get("error_code", "host_request_failed") + ": " + response.get("error", ""))
                    return response["result"]
                _forge_time.sleep(0.02)
            comm.close()
            _forge_comms.remove(comm)
            raise TimeoutError("host request timed out")

        class _ForgeRLM:
            def spawn(self, name, task): return _forge_request("rlm.spawn", {{"name": name, "task": task}})
            def list(self): return _forge_request("rlm.list", {{}})
            def cancel(self, child_id): return _forge_request("rlm.cancel", {{"child_id": child_id}})
            def delete(self, child_id): return _forge_request("rlm.delete", {{"child_id": child_id}})
            def shell(self, executable, *args, cwd=".", timeout_seconds=120):
                return _forge_request("rlm.shell", {{"executable": executable, "args": list(args), "cwd": cwd, "timeout_seconds": timeout_seconds}})

        class _ForgeMessages:
            def send(self, target, text): return _forge_request("agent_message.send", {{"target": target, "text": text}})
            def list(self): return _forge_request("agent_message.list", {{}})

        rlm = _ForgeRLM()
        agent_message = _ForgeMessages()
        session = _ForgeMappingProxyType({{"id": {json.dumps(session_id)}, "root_session_id": {json.dumps(root_session_id)}, "workspace": {json.dumps(workspace)}}})

        def _forge_shell_magic(line, cell=None):
            command_text = line if cell is None else (line + " " + cell)
            parts = _forge_shlex.split(command_text)
            if not parts: raise ValueError("forge_shell requires an executable")
            return rlm.shell(parts[0], *parts[1:])
        get_ipython().register_magic_function(_forge_shell_magic, "line_cell", "forge_shell")
        """
    )
