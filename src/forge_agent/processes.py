"""Cross-platform process-tree termination shared by execution backends."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Iterable
from contextlib import suppress

WINDOWS_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
WINDOWS_CREATE_NEW_PROCESS_GROUP = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def terminate_process_tree(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = 2.0,
) -> None:
    """Best-effort termination that never waits for a failed tree-kill command.

    Windows ``taskkill`` may itself fail (notably while a newly created process
    group is still starting).  Its return code is therefore authoritative: on a
    non-zero result we immediately fall back to recursively killing children
    when psutil is available and finally to ``Popen.kill``.
    """

    if process.poll() is not None:
        return

    tree_killed = False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=max(grace_seconds, 0.1),
                shell=False,
                creationflags=WINDOWS_CREATE_NO_WINDOW,
            )
            tree_killed = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            tree_killed = False
    else:
        try:
            os.kill(-process.pid, signal.SIGTERM)
            tree_killed = True
        except OSError:
            tree_killed = False

    if tree_killed and _wait(process, grace_seconds):
        return

    _terminate_with_psutil(process.pid, grace_seconds=grace_seconds)
    if process.poll() is None:
        with suppress(OSError):
            process.kill()
    _wait(process, grace_seconds)


def _wait(process: subprocess.Popen[bytes], timeout: float) -> bool:
    try:
        process.wait(timeout=max(timeout, 0.1))
    except (OSError, subprocess.SubprocessError):
        return process.poll() is not None
    return True


def _terminate_with_psutil(pid: int, *, grace_seconds: float) -> None:
    """Recursively stop descendants when the optional RLM dependency exists."""

    try:
        import psutil
    except ImportError:
        return
    try:
        parent = psutil.Process(pid)
        processes: Iterable[psutil.Process] = (*parent.children(recursive=True), parent)
        alive = []
        for child in processes:
            try:
                child.terminate()
                alive.append(child)
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(alive, timeout=max(grace_seconds, 0.1))
        for child in alive:
            with suppress(psutil.Error):
                child.kill()
    except psutil.Error:
        return
