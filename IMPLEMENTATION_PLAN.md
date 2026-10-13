# ForgeAgent implementation and release plan

This document describes how ForgeAgent is developed, verified, and released. It is a living engineering plan rather than a chronological development diary. Public priorities that still require work are tracked in [docs/ROADMAP_PRIORITIES.md](docs/ROADMAP_PRIORITIES.md).

## Product scope

ForgeAgent provides two deliberately separate execution modes:

- **Safe coding mode** (`forge run`) uses bounded tools, deterministic policy checks, approvals, workspace confinement, structured traces, and optional Docker routing.
- **Trusted RLM mode** (`forge rlm ... --trust-workspace`) adds durable sessions, a persistent IPython kernel, child agents, message routing, and context compaction for explicitly trusted repositories.

The project owns its runtime, provider-neutral domain models, tool protocol, persistence format, and safety policy. Provider SDK objects and model claims never determine local authorization.

## Capability map

| Area | Implemented | Remaining validation |
| --- | --- | --- |
| Runtime | bounded model/tool loop, cancellation, stop reasons, streaming events | long-running real-provider soak tests |
| Providers | OpenAI-compatible, Anthropic, Ollama, scriptable Faux provider | native Anthropic streaming and multi-provider live regression |
| Repository tools | bounded listing, search, reads, exact patches, Git inspection, isolated worktrees by default, explicit in-place locking and stale-read detection | broader cross-platform repository corpus; external editors do not honor advisory locks |
| Command safety | `SAFE / REVIEW / DENY`, approvals, environment filtering, Docker routing | Docker/WSL host isolation tests |
| Observability | typed events, redaction, JSONL traces, usage and cost completeness | production telemetry export is intentionally out of scope |
| Durable RLM | sessions, queues, repair, kernel bridge, children, messages, compaction | one budget-capped real-provider acceptance run |
| Evaluation | deterministic catalog, reference executor, reports, coverage gate | reproducible live-model baselines |
| Packaging | editable install, wheel/sdist configuration, CI matrix | protected release-environment exercise |

“Implemented” means the behavior is covered by local automated tests. It does not imply that a simulated provider or container proves the corresponding external service or host isolation.

## Engineering workstreams

### 1. Core contracts

- Keep messages, tool calls, usage, stop reasons, events, and session entries provider-neutral.
- Treat serialized Pydantic models as compatibility-sensitive public contracts.
- Add fields in a backward-compatible way and preserve unknown session entry types during replay.
- Require stable IDs and explicit parent/root relationships for recursive work.

### 2. Runtime and context

- Keep one authoritative loop for model calls, tools, budgets, cancellation, and stopping.
- Restore every busy flag and resource in `finally` blocks.
- Preserve tool-call/result pairs when building or compacting context.
- Stop with a machine-readable failure when compaction cannot safely recover an overflow.

### 3. Providers

- Normalize text, tool calls, partial arguments, usage, retryable errors, and cancellation.
- Keep `generate()` compatible by collecting the common stream protocol.
- Never infer complete cost data when usage or pricing is unavailable.
- Use the Faux provider for deterministic offline tests, not as evidence of model quality.

### 4. Tools and security

- Resolve every filesystem target through the workspace boundary.
- Keep command input structured; never execute model-authored shell source.
- Make policy and backend selection host-owned and deterministic.
- Fail closed when an operation requires Docker and the sandbox is unavailable.
- Redact secrets before traces, errors, or artifacts are persisted.
- Default new run entry points to a separate Git worktree; never silently stash, discard, or copy source working-tree changes.
- For explicit in-place operation, require a host-observed read before patch, revalidate after approval under sorted advisory file locks, and force re-read/reapproval on conflicts.
- Keep worktree review and integration explicit: no automatic commit, merge, deletion, or push. Treat worktrees as working-copy separation, never a sandbox.

### 5. Sessions and trusted RLM

- Persist accepted input before acknowledging it.
- Maintain a single writer per session and append-only UTF-8 JSONL records.
- Never replay uncertain side effects after recovery.
- Bind Host Bridge identity to the kernel connection, not payload fields.
- Treat the kernel as trusted user-level code, never as a sandbox.

### 6. Evaluation and evidence

- Separate deterministic reference checks from live-model evaluation.
- Record model ID, provider, prompt/config version, task snapshot, repetitions, usage, known cost, latency, diff, and failure class for live runs.
- Label simulated HTTP, fake sandboxes, and Faux provider results explicitly.
- Update documentation metrics only after a complete local regression.

## Change workflow

### Latest local verification snapshot (2026-10-13)

- Environment: Windows, Python 3.12.9.
- Full test suite: 264 passed; coverage: 87.02% (80% gate).
- Ruff format/check, mypy (72 files), `pip check`, and `git diff --check`: passed.
- Offline reference harness: 20/20; this validates the harness, not model quality. Offline wheel/sdist build passed.
- These results are local and cover the current concurrency changes. They have not yet been exercised by GitHub CI; the historical CI result above reflects an earlier revision and must not be presented as validation of these changes.

1. Inspect the worktree and identify unrelated user changes.
2. Add or update focused tests before changing public schemas, configuration, CLI output, or security policy.
3. Implement the smallest coherent change and run the affected tests.
4. Run formatting, linting, type checking, the full coverage suite, dependency checks, build checks, and `git diff --check`.
5. Update architecture, security, configuration, changelog, and roadmap documentation where relevant.
6. Merge only when CI is green and the evidence accurately describes what was tested.

## Release gates

A release candidate must satisfy all of the following:

```powershell
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest --cov=forge_agent --cov-report=term-missing --cov-fail-under=80 -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m build --no-isolation
git diff --check
```

Additional gates:

- no credentials, local session artifacts, generated reports, or target repositories in the package;
- the safe and trusted execution modes remain visibly distinct;
- isolated runs leave the source checkout unchanged, while in-place runs reject another ForgeAgent writer and detect stale patch bases;
- workspace diff output is bounded and excludes sensitive files and symlinks; no worktree lifecycle command silently deletes or merges user work;
- new external side effects have deterministic policy, approval, audit, and recovery behavior;
- wheel and sdist install successfully in a clean environment;
- documentation contains no machine-specific or unverifiable claims.

## Version direction

- **0.3.x:** stabilize durable sessions and trusted RLM, finish real-provider and Docker evidence.
- **0.4.x:** improve provider streaming, live evaluation repeatability, and release automation.
- **Later:** consider supervisor/worker processes, richer code intelligence, and editor integration only when measured failures justify the complexity.

Vector databases, automatic push/deploy, unrestricted shell access, Docker socket mounting, and automatic system-component installation are outside the default scope.

## Definition of done

A capability is complete only when its code, tests, user documentation, safety boundaries, error behavior, and observable evidence agree. Work that depends on credentials, provider controls, Docker hosts, or protected GitHub environments stays on the public roadmap until the external verification is performed.
