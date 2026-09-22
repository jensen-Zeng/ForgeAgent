"""Deterministic synthetic ablation for M09 context strategies.

This measures context-size proxies, not model task success. M10 supplies real
task scoring. Keeping the two claims separate avoids presenting synthetic data
as an LLM quality benchmark.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Task:
    name: str
    observation_chars: int
    observations: int
    duplicate_every: int
    repeated_failures: int


@dataclass(frozen=True)
class Result:
    task: str
    strategy: str
    estimated_tokens: int
    observations_kept: int
    stopped_no_progress: bool


TASKS = tuple(
    Task(
        name=f"context-{index:02d}",
        observation_chars=600 + index * 137,
        observations=5 + index % 4,
        duplicate_every=2 + index % 3,
        repeated_failures=1 + index % 4,
    )
    for index in range(1, 11)
)


def run_task(task: Task, strategy: str) -> Result:
    """Apply a transparent size model for one strategy."""
    observations = task.observations
    stopped = False
    per_observation = task.observation_chars
    if strategy in {"dedup", "dedup_compress", "full"}:
        unique = math.ceil(observations / task.duplicate_every)
        duplicate_references = observations - unique
        chars = unique * per_observation + duplicate_references * 60
    else:
        chars = observations * per_observation
    if strategy in {"dedup_compress", "full"}:
        preserved = min(2, observations)
        old = max(observations - preserved, 0)
        chars = min(chars, preserved * per_observation + old * 90)
    if strategy == "full" and task.repeated_failures >= 3:
        stopped = True
        observations = min(observations, 3)
        chars = min(chars, observations * per_observation)
    return Result(
        task=task.name,
        strategy=strategy,
        estimated_tokens=math.ceil((chars + 1200) / 4),
        observations_kept=observations,
        stopped_no_progress=stopped,
    )


def main() -> None:
    """Print stable JSON for reports and regression checks."""
    strategies = ("baseline", "dedup", "dedup_compress", "full")
    results = [run_task(task, strategy) for task in TASKS for strategy in strategies]
    summary = {
        strategy: {
            "mean_estimated_tokens": round(
                sum(result.estimated_tokens for result in results if result.strategy == strategy)
                / len(TASKS),
                1,
            ),
            "no_progress_stops": sum(
                result.stopped_no_progress for result in results if result.strategy == strategy
            ),
        }
        for strategy in strategies
    }
    print(
        json.dumps(
            {
                "method": "synthetic context-size proxy; not model success",
                "tasks": len(TASKS),
                "summary": summary,
                "results": [asdict(result) for result in results],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
