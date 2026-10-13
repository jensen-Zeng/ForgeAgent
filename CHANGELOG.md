# Changelog

All notable changes to ForgeAgent are documented here. The project follows semantic versioning once a stable release is published; development versions may still evolve public interfaces.

## Unreleased

### Changed

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
- Native Anthropic streaming and real-provider RLM acceptance remain on the roadmap.
- Docker isolation behavior requires validation on each supported host configuration.
