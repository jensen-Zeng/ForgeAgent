"""Tests for the resettable M10 evaluation catalog and reports."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_agent.eval_catalog import build_task_catalog
from forge_agent.evaluation import (
    EvalExecution,
    EvalTaskInput,
    EvaluationRunner,
    ReferenceExecutor,
    load_report,
    write_report,
)


def test_catalog_has_twenty_tasks_with_expected_category_distribution() -> None:
    tasks = build_task_catalog()
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.category] = counts.get(task.category, 0) + 1

    assert len(tasks) == 20
    assert len({task.id for task in tasks}) == 20
    assert counts == {
        "bug_fix": 6,
        "add_test": 3,
        "feature": 4,
        "refactor": 3,
        "readonly": 2,
        "safety": 2,
    }
    assert all(task.public_test_command for task in tasks)
    assert all(task.hidden_checks for task in tasks)


def test_public_task_input_excludes_hidden_checks_and_reference_answers() -> None:
    public = build_task_catalog()[0].public_input()

    assert isinstance(public, EvalTaskInput)
    assert not hasattr(public, "hidden_checks")
    assert not hasattr(public, "reference_files")
    assert not hasattr(public, "initial_files")


@pytest.mark.asyncio
async def test_reference_baseline_scores_all_twenty_tasks() -> None:
    tasks = build_task_catalog()
    report = await EvaluationRunner(
        tasks=tasks,
        executor=ReferenceExecutor(tasks),
        version="test",
        git_commit="abc123",
        model="reference-harness",
    ).run()

    assert report.task_count == 20
    assert report.summary.runs == 20
    assert report.summary.successes == 20
    assert report.summary.success_rate == 1.0
    assert report.summary.failure_types == {}
    assert {record.category for record in report.records} == {
        "bug_fix",
        "add_test",
        "feature",
        "refactor",
        "readonly",
        "safety",
    }


@pytest.mark.asyncio
async def test_each_repetition_starts_from_fresh_snapshot() -> None:
    task = build_task_catalog()[0]

    class ResetCheckingExecutor:
        calls = 0

        async def execute(
            self,
            task_input: EvalTaskInput,
            workspace: Path,
        ) -> EvalExecution:
            del task_input
            assert not (workspace / "run-marker.txt").exists()
            (workspace / "run-marker.txt").write_text("seen", encoding="utf-8")
            ResetCheckingExecutor.calls += 1
            return EvalExecution(
                status="failed",
                stop_reason="intentional",
                duration_seconds=0,
            )

    report = await EvaluationRunner(
        tasks=(task,),
        executor=ResetCheckingExecutor(),
        version="test",
        git_commit="abc",
        model="reset-check",
    ).run(repetitions=3)

    assert ResetCheckingExecutor.calls == 3
    assert report.summary.runs == 3


@pytest.mark.asyncio
async def test_failure_type_prefers_executor_terminal_reason() -> None:
    task = build_task_catalog()[0]

    class FailingExecutor:
        async def execute(
            self,
            task_input: EvalTaskInput,
            workspace: Path,
        ) -> EvalExecution:
            del task_input, workspace
            return EvalExecution(
                status="stopped",
                stop_reason="max_steps",
                duration_seconds=0.1,
                steps=3,
            )

    report = await EvaluationRunner(
        tasks=(task,),
        executor=FailingExecutor(),
        version="test",
        git_commit="abc",
        model="fake",
    ).run()

    record = report.records[0]
    assert record.success is False
    assert record.failure_type == "max_steps"
    assert report.summary.failure_types == {"max_steps": 1}


@pytest.mark.asyncio
async def test_constraints_are_scored_after_public_and_hidden_checks() -> None:
    task = next(task for task in build_task_catalog() if task.id == "readonly-01")

    class OverEditingExecutor:
        async def execute(
            self,
            task_input: EvalTaskInput,
            workspace: Path,
        ) -> EvalExecution:
            del task_input
            (workspace / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
            return EvalExecution(
                status="completed",
                stop_reason="completed",
                duration_seconds=0,
            )

    report = await EvaluationRunner(
        tasks=(task,),
        executor=OverEditingExecutor(),
        version="test",
        git_commit="abc",
        model="fake",
    ).run()

    record = report.records[0]
    assert record.public_test_passed is True
    assert record.hidden_checks_passed == record.hidden_checks_total
    assert record.failure_type == "constraint_failed"
    assert record.changed_files == 1
    assert record.added_lines == 1


@pytest.mark.asyncio
async def test_json_markdown_and_baseline_delta_round_trip(tmp_path: Path) -> None:
    tasks = build_task_catalog()[:2]
    first = await EvaluationRunner(
        tasks=tasks,
        executor=ReferenceExecutor(tasks),
        version="0.1",
        git_commit="first",
        model="reference",
        run_metadata={"prompt_version": "test-1"},
    ).run()
    second = await EvaluationRunner(
        tasks=tasks,
        executor=ReferenceExecutor(tasks),
        version="0.2",
        git_commit="second",
        model="reference",
        run_metadata={"prompt_version": "test-2"},
    ).run(baseline=first)

    json_path, markdown_path = write_report(second, tmp_path)
    restored = load_report(json_path)
    markdown = markdown_path.read_text(encoding="utf-8")

    assert restored == second
    assert restored.baseline_delta["success_rate"] == 0
    assert restored.baseline_delta["known_cost"] == 0
    assert restored.run_metadata["prompt_version"] == "test-2"
    assert "# ForgeAgent Evaluation Report" in markdown
    assert "Success: 2/2" in markdown
    assert "Git commit: `second`" in markdown
    assert "prompt_version" in markdown
