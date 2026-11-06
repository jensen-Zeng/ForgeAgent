# Changelog

All notable changes to ForgeAgent are documented here. The project follows semantic versioning once a stable release is published; development versions may still evolve public interfaces.

## Unreleased

### Added

- Host-enforced read-only `forge plan`, with an inspection/planning prompt and no patch, command, or Kernel tools.
- Scoped repository instructions: `AGENTS.override.md` / `AGENTS.md` / `CLAUDE.md` precedence, bounded root-to-target discovery, and source/truncation metadata in read/list results.
- Native Anthropic SSE text/tool argument aggregation, usage, stream error handling, and cancellation cleanup.
- Opt-in `--stream` and rate-limited progress in safe CLI runs/plans; RLM model/tool events now persist with turn/session correlation and live progress. Raw fragments are not printed or journaled as delta content. Non-streaming safe requests remain the default, retaining configured retries.
- A [design comparison](docs/DESIGN_COMPARISON.md) explaining priorities relative to Codex and Claude Code.

### Fixed

- Session compaction includes tool error details in its summary input, and its module can be imported independently of session initialization.
- Failed Python kernel calls retain bounded stdout, traceback, and artifact references, and provider message conversion preserves failure output alongside the error.
- OpenAI-compatible streaming preserves cached-token pricing and derives missing totals consistently with non-streaming responses; malformed usage returns a provider error.
- Cancellation now waits for owned model/tool operations to finish cleanup before returning, including cancelled child sessions.
- Boundary cancellation retains completed tool results and avoids starting another model request.
- Context compression invalidates duplicate-result references to observations that are no longer available in full.
- Platform-specific file-lock typing that prevented Linux CI from reaching tests.
- CI now installs into an isolated virtual environment, avoiding conflicts with hosted-runner global tools such as `pipx`.
- Git read-only tools disable optional index writes, external diff/textconv, and fsmonitor, and reject paths using configured executable filters.

### Changed

- Kept maintainer handoff and implementation notes local; public contributor guidance now includes release checks.
- Updated verification status with the Windows successes and Linux typing failures from the concurrency CI run, and clarified historical experiment/report evidence.
- Reframed public documentation around ForgeAgent as an independent open-source product.
- Consolidated architecture, implementation, security, and verification guidance.
- `forge run` and `forge rlm run` now use an isolated Git worktree by default; use `--in-place` to explicitly share the selected checkout.
- Added `forge workspace create/list/diff` to create, inspect, and review retained managed worktrees.

### Security

- Added cross-process workspace/run and per-file advisory locks, host-observed file-version checks, read-before-patch enforcement, and explicit stale-write errors for shared-checkout editing.
- Added exclusive RLM session lifetime ownership to prevent duplicate resume/repair; shutdown waits for active work to unwind while preserving not-yet-started queued inputs for recovery.
- Isolated worktrees require a committed, clean source checkout (allowing `.forge/` data) and do not copy or discard uncommitted changes.
- Worktree isolation is a working-copy boundary, not a sandbox; editor processes do not honor ForgeAgent advisory locks.

## 0.3.0.dev0

### Added

- Provider-neutral turn engine with streaming events, cancellation, usage, and explicit stop reasons.
- OpenAI-compatible, Anthropic, Ollama, and deterministic Faux providers.
- Bounded repository, patch, Git, and structured command tools.
- Deterministic `SAFE / REVIEW / DENY` policy with Docker auto-routing and fail-closed behavior.
- Durable JSONL sessions, ordered input queue, explicit tail repair, and recovery markers.
- Opt-in trusted RLM mode with persistent IPython, Host Bridge, child agents, durable family messages, and structured context compaction.
- Reference evaluation catalog, reports, cross-platform CI, type checking, linting, and coverage gates.

### Security

- Workspace containment, sensitive-path rejection, environment filtering, trace redaction, exact patch review, and process-tree cleanup.

### Known limitations

- Trusted RLM kernels inherit the current user's permissions and are not sandboxes.
- Real-provider RLM and streaming acceptance remain on the roadmap; offline protocol tests do not establish live model quality.
- Docker isolation behavior requires validation on each supported host configuration.
