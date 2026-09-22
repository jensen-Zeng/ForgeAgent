# Roadmap priorities

Last updated: 2026-09-22
Current development line: `0.3.0.dev0`

This document contains only work that still needs implementation or external verification. Completed capabilities are described in the README, architecture, and changelog.

## P0 — release evidence and safety validation

| Item | Acceptance criteria |
| --- | --- |
| Real-provider RLM acceptance | With a provider-side hard spending limit, run one complete trusted RLM session; record session ID, provider/model, configuration revision, usage, known cost, completeness flag, stop reason, and failures. |
| Docker host validation | Build the base image and verify no network, non-root execution, read-only root, minimal mounts, CPU/memory/PID limits, timeout cleanup, working directory behavior, and inability to read host credentials. |
| Offline dependency image | Lock and review one Python target's dependencies, build a wheelhouse-derived image, and pass its tests without runtime network access. |
| Provider regression | Exercise at least two provider families three times each with a fixed task snapshot and budget; retain latency, usage, diff, and failure evidence. |
| Cost hard-limit validation | Confirm the provider rejects work beyond its configured account/project limit; compare this with ForgeAgent's local usage ledger. |

## P1 — protocol and release maturity

| Item | Acceptance criteria |
| --- | --- |
| Native Anthropic streaming | Validate text/tool deltas, cancellation, usage, rate limits, incomplete frames, and context overflow against the live protocol while preserving `generate()` compatibility. |
| Live RLM delta display | Render incremental output, cancel within ten seconds, and keep terminal output consistent with persisted events and the final aggregate. |
| Reproducible live benchmarks | Fix model, prompt/config revision, task snapshot, parameters, and three repetitions; report success, P95 latency, steps, tokens, known cost, diff, and failure classes. |
| Protected release workflow | Exercise a protected GitHub release environment, approval gate, wheel/sdist checks, hashes, and a draft release. |
| Dependency supply-chain evidence | Generate an SBOM and hash inventory for offline wheels and record vulnerability-scan results. |

## P2 — measurement-driven extensions

Start these only when evaluation identifies the stated bottleneck.

| Candidate | Trigger |
| --- | --- |
| Supervisor/worker runtime | Trusted RLM tasks show that single-process scheduling is the dominant limitation; design attach/detach, leases, journal, goals, heartbeat, and crash recovery first. |
| AST, symbol index, or LSP | Text search and directory summaries are a measured source of retrieval failure. |
| Additional language toolchains | Each language has locked dependencies, a narrow command policy, a maintained image, and security tests. |
| Prompt experiment management | Enough repeated live runs exist for meaningful comparisons. |
| MCP adaptation | The local tool protocol is stable and MCP permissions can be mapped to existing approval semantics. |
| TUI or editor integration | CLI output, approval behavior, and result schemas are stable. |

## Deferred

- Vector-database memory until durable summaries and repository search are measured bottlenecks.
- Automatic pull requests, pushes, and deployment until identity, permissions, approval, and audit have a separate threat model.
- Large benchmark suites until smaller live baselines are reproducible and budgeted.
- Automatic installation of Docker, WSL, models, or system components.
- Default shell access, container networking, Docker socket access, or home-directory mounts.
