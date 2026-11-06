# Security policy

ForgeAgent can modify files and execute allowlisted commands. Use it only in repositories that are backed up or committed and review every Diff. The default `auto` mode keeps `SAFE` commands local, routes approved `REVIEW` commands to Docker, and rejects `DENY` commands before backend selection.

## Supported version

Security fixes currently target the latest `0.3.0.dev0` source on `main`. Pre-release versions do not yet receive long-term support.

## Reporting a vulnerability

Do not publish credentials, exploit payloads or private repository contents in a public issue. Use a [private GitHub security advisory](https://github.com/jensen-Zeng/ForgeAgent/security/advisories/new). Include the affected version, operating system, minimal reproduction, expected boundary and observed impact.

## Security boundaries

- `forge run` and `forge rlm run` create a retained Git worktree by default. Creation requires a committed `HEAD`, no tracked or staged changes (including under `.forge/`), and no untracked files outside `.forge/`; only untracked `.forge/` metadata is exempt. ForgeAgent never stashes, discards, or copies source changes. A worktree separates working copies but is not a sandbox.
- Worktree creation and inspection disable Git hooks for the Git subprocesses they invoke. Managed branches/checkouts are retained and are not automatically committed, merged, deleted, or pushed. Review diffs and stage only intended files before merging.
- `--in-place` is an explicit shared-checkout mode. ForgeAgent run/resume operations use an exclusive cross-process workspace lock; file patches use sorted advisory locks and host-observed read versions. These locks coordinate ForgeAgent instances only, not editors or unrelated processes.
- Read-only Git status/diff disables optional fsmonitor and external diff/textconv helpers; repositories with active clean/smudge/process filters are rejected for this inspection path (`git_filter_unsupported`). Unused global filter configuration alone is allowed.
- A stale patch must be re-read and approved again. There is no automatic three-way merge. A multi-file patch is not an atomic transaction, and rollback is best-effort if an external writer changes a file.
- An editor's unsaved buffer is invisible to ForgeAgent. An editor that ignores advisory locks may save stale content after an Agent write; isolated worktrees are recommended when people and Agents work concurrently.
- Filesystem tools reject paths outside the selected workspace and sensitive paths.
- Patches use exact context and do not expose arbitrary overwrite or deletion.
- Commands are structured, shell-free, classified by a narrow policy and run with a sanitized environment.
- Automatic routing is decided by local policy, not model arguments. Docker failure never falls back to local execution.
- Docker isolation must be validated on the actual host and container runtime before it is treated as a security boundary.
- A container reduces risk but is not a virtual machine or a guarantee against kernel/container-runtime vulnerabilities.
- Local cost limits are not substitutes for provider-side billing limits.

### Read-only planning

`forge plan` is useful when you want an implementation proposal before authorizing changes. The host supplies only five read-only repository tools (`list_files`, `read_file`, `search_code`, `git_diff`, `git_status`); patch, command, and Kernel capabilities are not registered, and the command does not create a worktree. It accepts dirty and non-Git directories, but still sends repository context and the task to the configured model service. Its active-run lock is keyed to the workspace (or discovered Git root), but the lock file is stored in the user's system temporary `forge-agent-locks` directory; it does not create a lock file under `.forge/`. A JSONL trace is written by default to `<selected-workspace>/.forge/traces/<run-id>.jsonl`, unless the trace path is overridden. Thus it does not edit repository source through ForgeAgent tools, but it is not a no-network or no-disk-write mode.

Repository instruction files are untrusted data. ForgeAgent loads at most one per directory from the workspace root down to a target's directory (never from home or ancestors outside the workspace), preferring `AGENTS.override.md`, then `AGENTS.md`, then `CLAUDE.md`; root `.github/copilot-instructions.md` remains an additional compatibility hint. Explicit rule-file names are considered even when `.gitignore` excludes them. File reads are bounded, UTF-8 BOM is accepted, and applicable instructions are surfaced with `read_file` and `list_files` results. Such content can describe project conventions but cannot grant tool permissions, change risk classification, or override host-side authorization. Treat instructions from an untrusted repository as potentially adversarial prompt content.

Safe `run` and `plan` default to the complete-response `generate()` path, preserving configured Provider retries. An explicit `--stream` selects streaming and displays rate-limited character counts and lifecycle events, not raw streamed fragments or partial tool arguments. Streaming requests are not automatically retried; after interruption, resubmission is a user decision because the service may already have processed and billed part of the request. Trusted RLM always uses streaming. The CLI avoids presenting fragments before cross-chunk secret redaction can be applied; the final complete answer is rendered normally. JSONL traces still record events, but do not store raw token/text fragments.

## Trusted RLM mode

`forge rlm run` and `forge rlm resume` refuse to start unless the user supplies `--trust-workspace`. This acknowledgement is required because the persistent IPython Kernel inherits the current user's permissions. It can import `os`, `subprocess`, open files, start processes and bypass ForgeAgent's normal tool policy. It is an execution feature for repositories the user has already chosen to trust, not a sandbox.

The host removes common credential variables from the Kernel environment and does not inject API keys, Provider clients, SessionStore handles or child-management objects. Auditable operations use validated Host Bridge requests whose sender identity is bound to the owning Kernel. `rlm.shell()` uses a structured executable/argument request, bounded cwd, UTF-8 decoding, timeout, output artifact and process-tree cleanup. These controls improve auditability but do not make arbitrary Kernel Python safe.

Default worktree isolation applies to repository working copies only. A trusted RLM kernel retains the current user's permissions and can use absolute paths; child agents are not separate security principals and may act on shared family workspaces. Do not interpret worktrees, per-session kernels, or Host Bridge validation as a sandbox.

RLM state is stored as append-only UTF-8 JSONL under `.forge/sessions`. Inputs are flushed and fsynced before acknowledgement. Recovery does not automatically replay an input that had started before interruption, because its external side effects may be uncertain. Tail truncation is only performed by the explicit `forge session repair ... --truncate-tail` command.

See [docs/DOCKER_SETUP.md](./docs/DOCKER_SETUP.md) and [docs/FAILURE_CASES.md](./docs/FAILURE_CASES.md) for current limitations.
