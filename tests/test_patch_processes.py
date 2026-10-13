"""Spawned-process contention tests for same-file patch transactions."""

from __future__ import annotations

import multiprocessing
import tempfile
from pathlib import Path

from forge_agent.domain import ToolCall
from forge_agent.tools import ApplyPatchTool, Workspace
from forge_agent.tools.patch import PatchReview


def _apply_competing_patch(
    workspace: str,
    temp_root: str,
    replacement: str,
    approval_barrier: object,
    results: object,
) -> None:
    tempfile.tempdir = temp_root
    root = Path(workspace)

    def approve(_: PatchReview) -> bool:
        approval_barrier.wait(timeout=15)  # type: ignore[attr-defined]
        return True

    tool = ApplyPatchTool(
        Workspace(root),
        approval_handler=approve,
        approval_mode="always",
        lock_timeout=5,
    )
    call = ToolCall(
        id=f"patch-{replacement}",
        name="apply_patch",
        arguments={
            "edits": [
                {
                    "path": "shared.py",
                    "old_text": "VALUE = 1",
                    "new_text": f"VALUE = {replacement}",
                }
            ]
        },
    )
    result = tool.execute(call)
    results.put((result.ok, result.metadata.get("error_code")))  # type: ignore[attr-defined]


def test_competing_approved_process_patches_only_allow_one_commit() -> None:
    project_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix=".test-patch-process-", dir=project_root) as raw:
        workspace = Path(raw)
        temp_root = workspace / "system-temp"
        temp_root.mkdir()
        (workspace / "shared.py").write_text("VALUE = 1\n", encoding="utf-8")

        context = multiprocessing.get_context("spawn")
        approval_barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_apply_competing_patch,
                args=(
                    str(workspace),
                    str(temp_root),
                    replacement,
                    approval_barrier,
                    results,
                ),
            )
            for replacement in ("2", "3")
        ]
        for process in processes:
            process.start()
        try:
            for process in processes:
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join(5)

            assert [process.exitcode for process in processes] == [0, 0]
            outcomes = [results.get(timeout=2) for _ in processes]
            assert sum(ok for ok, _ in outcomes) == 1
            assert sorted(code for ok, code in outcomes if not ok) == ["concurrent_modification"]
            assert (workspace / "shared.py").read_text(encoding="utf-8") in {
                "VALUE = 2\n",
                "VALUE = 3\n",
            }
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            results.close()
