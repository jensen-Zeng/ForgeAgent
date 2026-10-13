# Concurrent editing and isolated workspaces

ForgeAgent provides two ways to edit a repository. New `forge run` and `forge rlm run` commands use an isolated Git worktree by default. Use `--in-place` only when the task must operate in the selected checkout itself. This default keeps multiple Agent runs and ordinary edits in the source checkout from racing over the same working files.

## Default: isolated worktree

Before creating an isolated checkout, the source must be the top-level directory of a Git repository with a commit, no tracked or staged changes (including under `.forge/`), and no untracked files outside `.forge/`. Only untracked `.forge/` metadata is exempt. ForgeAgent does not stash, discard, or copy uncommitted source edits. Commit or move those edits yourself, or explicitly choose `--in-place` if you accept the shared-workspace behavior.

Managed worktree creation checks tracked source paths for an effective Git `filter` attribute backed by a configured external clean/smudge/process command. Before running any Git diff, worktree diff preflights every tracked path in the checkout, tracked path in the base commit, and untracked path that could be considered for the diff. This fail-closed check happens before even a name-only diff, because Git may invoke clean filters while normalizing file content. Git LFS repositories using such filters are therefore unsupported in managed-worktree mode. Use `--in-place` only if the workflow must retain those filters and you understand the consequences. This is not a safe execution sandbox: in-place mode does not isolate filter commands or other code from the current user's permissions.

Each run creates a retained checkout at:

```text
<source>/.forge/worktrees/<uuid>/checkout
```

and a branch named `forge/<uuid>`, based on the clean source `HEAD`. The Agent writes to this checkout. Changes a person makes and saves in the source checkout while the Agent runs do not change the Agent's checkout. The worktree is isolation between working copies, not an operating-system sandbox: code executed by the Agent still has the permissions of the ForgeAgent process.

Example:

```powershell
forge run "Fix the parser tests" --workspace D:\src\parser
forge workspace list --workspace D:\src\parser
forge workspace diff <WORKTREE_ID> --workspace D:\src\parser
```

The run prints its worktree ID, checkout path, and branch. Bounded repository tools operate on this checkout, and saved changes in the source working files do not appear in the Agent checkout. ForgeAgent does create worktree metadata under the source's `.forge/worktrees/` and Git worktree/branch metadata. It does not automatically commit, merge, delete, or push the Agent branch. This is not an operating-system boundary: in particular, trusted RLM Python may access absolute paths outside the checkout with the current user's permissions.

Review and integrate explicitly:

```powershell
forge workspace diff <WORKTREE_ID> --workspace <source-repository>
git -C <worktree-checkout> status --short
git -C <worktree-checkout> add <reviewed-paths>
git -C <worktree-checkout> commit -m "Describe the change"
git -C <source-repository> merge forge/<WORKTREE_ID>
```

Stage only reviewed paths. Resolve any Git merge conflicts yourself; ForgeAgent does not perform a three-way merge. Before merging, save open editor buffers in the source checkout, inspect its status, and commit or otherwise safely set aside source-side edits that overlap the Agent branch. The merge may conflict if both branches changed the same lines.

Worktrees are intentionally retained so their diffs and branches remain reviewable. Removal is a manual Git operation; do not delete the managed directory behind Git's back. The checkout has its own `.forge` session and trace data. Use the checkout path when resuming or inspecting a session created by an isolated run:

```powershell
forge rlm resume <SESSION_ID> --workspace <worktree-checkout> --trust-workspace
forge session list --workspace <worktree-checkout>
forge session show <SESSION_ID> --workspace <worktree-checkout>
```

ForgeAgent does not copy ignored files, local virtual environments, or other untracked `.gitignore` data from the source into a new checkout. Configure provider credentials in the host process as usual; do not rely on a repository-local `.env` being copied or loaded.

Configuration is loaded by the host before ForgeAgent switches its bounded tools to the checkout. The selected `forge.toml` path remains a host-side input and is not reloaded from the checkout. If `forge.toml` is tracked, Git naturally includes its committed version in the worktree; ForgeAgent does not separately copy the selected config file. Explicit `--config` paths remain host-side configuration inputs.

## Shared checkout: `--in-place`

`--in-place` explicitly opts out of worktree isolation:

```powershell
forge run "Fix the parser tests" --workspace <repository> --in-place
forge rlm run "Analyze the failure" --workspace <repository> --trust-workspace --in-place
```

This mode is useful for non-Git folders and workflows that require the existing checkout, but it needs stronger coordination:

- A cross-process workspace lock allows only one ForgeAgent `run` or `resume` operation on that canonical workspace at a time. A second operation fails with `workspace_busy` instead of running concurrently. Each RLM session also has a lifetime ownership lock that prevents duplicate `resume` or `repair` access. Shutdown requests cancellation, waits for the active turn to unwind, and only then releases ownership; durable inputs not yet started remain queued for later recovery.
- In the production tool registry, a file must first be read through `read_file` before it can be patched. The host records the observed file version and SHA-256. A patch can optionally include `base_sha256`; if supplied, it must match the file version.
- After approval, patches acquire advisory cross-process locks for all target files in sorted order, then re-check the observed version and exact old text before writing. A stale observation returns `stale_read`; a missing required read returns `read_required`; a lock timeout returns `write_lock_timeout`; an intervening change returns `concurrent_modification`.
- On these conflicts, the Agent must read the current file again, prepare a new patch, and obtain approval again. ForgeAgent does not guess, auto-rebase, or perform a three-way merge.
- New-file creation uses atomic create-if-absent semantics. Individual file replacement is atomic, but a multi-file patch is not a filesystem transaction. If a later file fails, rollback is best-effort and avoids overwriting a file whose contents changed externally.

These locks are advisory and coordinate ForgeAgent processes that use the same locking implementation. Editors and unrelated programs do not honor them. A person should save and close or avoid editing the same file during an in-place Agent write. Unsaved editor buffers are invisible to ForgeAgent, and an editor can still save stale contents after the Agent writes. There remains a narrow time-of-check/time-of-use window for applications that ignore the advisory lock. For concurrent human and Agent work, the isolated-worktree default is the safer workflow.

## Choosing a mode

| Situation | Recommended mode |
| --- | --- |
| Two Agent tasks may touch the same repository | Default isolated worktrees; review each diff and merge deliberately |
| A person edits the source while an Agent runs | Default isolated worktree; source edits remain separate from the Agent checkout |
| Task needs an existing non-Git directory or must use the current checkout | `--in-place`, with one writer and frequent saves/backups |
| Trusted RLM task | Default isolated worktree is still not a sandbox; use only with trusted code |

The RLM kernel may use absolute paths and arbitrary Python with the current user's permissions. Child agents have separate session/kernel state, but they belong to the same trusted family and may act on shared family workspaces; do not treat them as mutually isolated security principals.
