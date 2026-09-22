# End-to-end walkthrough

This walkthrough demonstrates ForgeAgent's user-facing workflow and the evidence it produces. Use a small repository with no secrets, a clean Git state, and fast tests.

## Prepare

1. Set a provider-side spending limit.
2. Configure the provider environment variables.
3. Run `forge config check`, `forge doctor`, and the target repository's tests.
4. Keep a second terminal ready for Git diff and trace inspection.

## Safe coding flow

```powershell
.\.venv\Scripts\forge.exe config check
.\.venv\Scripts\forge.exe doctor
.\.venv\Scripts\forge.exe run "Fix the failing test with the smallest justified change" --workspace <demo-repository>
git -C <demo-repository> diff --check
git -C <demo-repository> diff
```

Observe:

- the selected workspace and configured budgets;
- bounded repository reads and searches;
- tool argument validation and policy decisions;
- exact patch preview and approval;
- final tests, diff, stop reason, usage, and cost-completeness flag;
- redacted JSONL events for later inspection.

If Docker is unavailable, reviewed commands under the default automatic backend return `sandbox_unavailable`; ForgeAgent does not silently run them on the host.

## Trusted RLM flow

Use this only with a repository you trust:

```powershell
.\.venv\Scripts\forge.exe rlm run "Analyze the failures and delegate independent source and test inspection" --workspace <trusted-repository> --trust-workspace
.\.venv\Scripts\forge.exe session show <session-id> --workspace <trusted-repository>
```

Inspect persistent kernel state, child lifecycle entries in `family.jsonl`, durable messages, shared usage, and compaction records. The trust flag starts a user-permission IPython kernel; it does not enable a sandbox.

## Evidence rules

- Faux provider tests demonstrate deterministic runtime behavior, not model quality.
- Fake Docker runners demonstrate policy and command construction, not host isolation.
- Reference evaluation demonstrates evaluator behavior, not autonomous task success.
- Live results should identify the provider, model, configuration, repository revision, repetitions, token usage, known cost, and failure class.
