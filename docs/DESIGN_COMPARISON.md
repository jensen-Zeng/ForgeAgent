# Design review: Codex, Claude Code, and ForgeAgent

Review date: 2026-11-07. This compares documented workflows, not private implementations or model quality. ForgeAgent remains a small, auditable local agent rather than a replacement for either product.

## What matters most for this project

| Priority | Design reference | Gap in ForgeAgent | Implemented change |
| --- | --- | --- | --- |
| 1 | Claude Code separates planning from editing; Codex recommends explicit plans and verification for longer tasks. | A request to “only plan” previously relied on the prompt while write tools remained available. | `forge plan` exposes only repository reads and Git status/diff. Patch, command, and Python execution tools are absent at the host boundary. |
| 2 | Codex resolves repository instructions by directory and precedence. Claude Code discovers scoped instructions when relevant files are read. | Root-only rules missed component conventions in larger repositories. | Bounded root-to-target instruction discovery, surfaced by `read_file` and `list_files`, with source paths and truncation metadata. |
| 3 | Both products expose ongoing agent activity; Anthropic's Messages protocol provides incremental content and tool arguments. | Anthropic returned only an aggregate, and RLM model/tool events did not reach the session journal or terminal. | Native Anthropic SSE, opt-in `--stream` in safe CLI runs/plans, and correlated RLM model/tool lifecycle events with live progress counters. |

Sources: [Claude Code planning workflow](https://code.claude.com/docs/en/common-workflows#plan-before-editing), [Codex long-running tasks](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex), [Codex repository instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md), [Claude Code memory and scoped instructions](https://code.claude.com/docs/en/memory), and [Anthropic streaming protocol](https://platform.claude.com/docs/en/build-with-claude/streaming).

The priorities above are this project's assessment: reducing accidental writes and context mistakes is more valuable now than adding another orchestration layer. Existing worktrees, approvals, durable sessions, bounded tools, and opt-in trusted Python already provide the foundation.

## Resulting workflow

```powershell
forge plan "Investigate the parser and propose a fix with tests" --workspace .
# Review the proposal, then explicitly request implementation:
forge run "Implement the reviewed parser fix and its tests" --workspace .
```

Planning reads the selected checkout, including uncommitted changes; it does not create a worktree or apply the proposal. Its local lock and trace still write runtime metadata. A plan is advisory, not a signed artifact or automatic approval for a later run. Implementation still uses the normal worktree, patch approval, and command policies.

For each relevant directory, one instruction file is selected: `AGENTS.override.md`, otherwise `AGENTS.md`, otherwise `CLAUDE.md`. Ancestors appear before descendants. Rules are workspace-local, bounded, and advisory: they cannot grant tools or override host policy. This is deliberately a subset of upstream configuration systems, without global home-directory discovery, arbitrary imports, or executable hooks.

Streaming providers still produce one authoritative completed response before any tool executes. Progress output reports character counts rather than raw token fragments: a secret split between chunks must not bypass redaction. Events carry session/turn identifiers for correlation. This is not a token-by-token chat renderer. Streaming does not automatically retry requests; callers must decide whether to rerun after a transport/protocol failure. Safe `run` and `plan` therefore retain non-streaming requests and their configured retry behavior by default; `--stream` explicitly selects incremental progress. RLM continues to use streaming.

## Evidence and remaining limits

- Offline tests cover read-only tool exclusion, scoped instructions, SSE aggregation/error/cancellation, early progress, and persisted tool lifecycle correlation.
- Local regression: 299 tests passed, 86.91% coverage (Windows / Python 3.12.9). The [CI run for `999edd9`](https://github.com/jensen-Zeng/ForgeAgent/actions/runs/42251334137) passed all four Windows/Linux × Python 3.12/3.13 jobs. Wheel/sdist build and metadata checks passed; these are implementation checks, not model-quality scores.
- No live model comparison or spending-limit test is claimed. Real-provider acceptance and Docker host validation remain in the [roadmap](ROADMAP_PRIORITIES.md).
- MCP, IDE/TUI integration, persistent global memory, and supervisor/worker scheduling remain measurement-driven extensions. They are not required to validate these changes.
