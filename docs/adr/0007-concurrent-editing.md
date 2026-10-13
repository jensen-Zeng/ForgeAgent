# ADR 0007: Isolated worktrees and guarded in-place edits

- Status: Accepted
- Date: 2026-10-13

## Context

Two ForgeAgent processes, or ForgeAgent and a person, can read the same file version and then attempt to write it. An exact text match helps reject stale patches, but without a shared lock there is still a check/write race. Directly sharing a working tree also makes review and recovery harder. Editors have unsaved buffers and do not participate in application-level locks, so ForgeAgent cannot fully coordinate with them.

## Decision

New `forge run` and `forge rlm run` commands create a retained Git worktree by default. Worktree creation requires the selected source to be the Git repository top-level, to have a commit, no tracked or staged changes (including under `.forge/`), and no untracked files outside `.forge/`; only untracked `.forge/` metadata is exempt. ForgeAgent does not stash, discard, copy ignored files, commit, merge, delete, or push. Each worktree has a UUID, a `forge/<uuid>` branch, a manifest, and a checkout under `.forge/worktrees/<uuid>/checkout`. The workspace CLI lists and reviews a bounded diff against the recorded base, including safe text untracked files.

Users review the isolated diff, stage selected files, commit in the worktree, and merge explicitly. Git conflicts are resolved by the user. Sessions created inside an isolated run live under that checkout, so session commands must point to the checkout path.

Managed worktree creation checks tracked source paths for an effective Git `filter` attribute backed by a configured external clean/smudge/process command. Before any diff command, diff preflight includes tracked checkout paths, tracked base-commit paths, and untracked candidates; it fails closed before even a name-only diff because Git may run clean filters while normalizing content. Repositories using such filters, including common Git LFS setups, are unsupported in managed-worktree mode. `--in-place` is an explicit compatibility alternative, not a sandbox or containment boundary for filter programs.

`--in-place` is an explicit shared-checkout option. CLI `run` and `resume` acquire a cross-process lock for the workspace. The production patch registry requires a prior host-observed read of an existing file. Patch application obtains canonical per-file advisory locks in stable sorted order after approval, then checks the recorded file digest, optional `base_sha256`, and exact old text before applying. Lock contention, missing read, stale version, and concurrent change have distinct machine-readable errors. A conflict requires a new read, patch, and approval.

New files use atomic create-if-absent. Individual replacements are atomic, but a multi-file patch is not a transaction. Rollback is best-effort and must not overwrite content that changed externally.

## Consequences

### Positive

- Separate Agent runs do not mutate the same checkout by default.
- Human edits saved in the source checkout do not silently enter or get overwritten by an Agent's isolated checkout.
- Diffs and work branches remain available for deliberate review and integration.
- Explicit in-place use prevents concurrent ForgeAgent runs and substantially reduces lost updates between ForgeAgent patch writers.
- Stale observations are surfaced rather than automatically rebased or merged.

### Tradeoffs and limits

- Worktree mode requires Git, a committed `HEAD`, no tracked or staged changes (including under `.forge/`), and no untracked files outside `.forge/`; only untracked `.forge/` metadata is exempt. Existing ignored dependencies such as a virtual environment are not copied.
- Managed worktree checkout/diff rejects active external Git filters on tracked source paths and every tracked checkout/base or untracked diff candidate rather than running them or disabling them; this includes common Git LFS repositories. Explicit in-place mode avoids managed-worktree operations but does not sandbox Git filters or workspace code.
- Worktrees accumulate until the user removes them through an intentional Git worktree lifecycle operation; ForgeAgent does not delete user work automatically.
- Per-file locks are advisory and only coordinate participants using ForgeAgent's lock protocol. Text editors and unrelated tools can still race. Unsaved buffers are invisible, and stale editor saves may replace newer disk contents.
- There is a narrow time-of-check/time-of-use window for non-cooperating writers. A multi-file patch may partially apply before a later write fails.
- The worktree is not a sandbox. In particular, a trusted RLM kernel can run arbitrary Python with the current user's permissions and access absolute paths. Child agents share the trusted family and are not separate security principals.
- Users must review diffs, commit selected files, and resolve Git merge conflicts themselves. ForgeAgent does not automatically merge or push.

## Validation requirements

- Worktree creation rejects dirty, uncommitted source changes without changing them.
- Two isolated runs use different checkouts and branches; source edits do not alter either checkout.
- Workspace diff is bounded and excludes sensitive paths, symlinks, binary content, and oversized files.
- In-place overlapping Agent patches serialize or return a stable lock conflict; stale reads require rereading and reapproval.
- New-file creation never overwrites an existing path.
- Documentation and CLI state clearly that worktrees are not sandboxes and do not coordinate external editors.
