# ForgeAgent beginner guide

This guide is for readers who know basic C and data structures but are new to Python, large language models, and agent runtimes. It explains the dependencies, the major modules, and a safe path from installation to contribution.

## 1. What ForgeAgent is

A normal chat model produces text. A coding agent repeatedly:

1. receives a programming task;
2. asks a model what to do next;
3. validates the model response;
4. runs an allowed tool, such as reading a file or searching text;
5. sends the observation back to the model;
6. stops when the task is complete or a budget/safety condition is reached.

The loop is similar to a state machine in C. The current state contains messages, budgets, tool results, and cancellation status. Each model or tool result causes a transition. ForgeAgent keeps those transitions explicit so they can be tested and audited.

## 2. Concepts mapped to C knowledge

| Familiar C concept | ForgeAgent/Python equivalent |
| --- | --- |
| `struct` | Pydantic model or `dataclass` |
| function pointer table | tool registry |
| enum and switch | stop reason, event kind, risk class |
| queue | durable turn input queue |
| tree | parent/child agent family |
| log file | append-only JSONL session or trace |
| process | model client, command, or IPython kernel |
| return code | structured tool result or stop reason |
| header/API contract | Python protocol and typed public model |

Python manages memory automatically, but resource lifetime still matters. Network responses, subprocesses, files, and kernels must be closed. ForgeAgent uses context managers and `try/finally` for the same reason C programs use a single cleanup path.

## 3. Required software

### Required

- **Python 3.12 or 3.13** — runs the application and test suite.
- **Git** — identifies repository state and displays changes.
- **pip and venv** — create an isolated Python environment and install dependencies.
- **A terminal** — PowerShell on Windows or a POSIX shell on Linux/macOS.

### Required for a real model

Choose at least one:

- an OpenAI-compatible HTTP service;
- Anthropic's API;
- a local Ollama server.

ForgeAgent does not ship a model. Credentials are supplied through the process environment and must never be committed.

### Optional

- **Docker** — required when `REVIEW` commands must run in a container under the default `auto` backend.
- **IPython/Jupyter dependencies** — installed by the `rlm` extra for trusted persistent-kernel mode.
- **An editor** — VS Code, PyCharm, or any editor with Python support.

## 4. Install the project

From the repository root on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -c requirements\constraints.txt -e ".[dev,rlm,release]"
.\.venv\Scripts\forge.exe doctor
```

On Linux or macOS, replace `.\.venv\Scripts\...` with `.venv/bin/...`.

A virtual environment is a private dependency directory for one project. It prevents ForgeAgent packages from changing the system Python environment.

## 5. Configure a model

OpenAI-compatible example:

```powershell
$env:FORGE_MODEL_PROVIDER = "openai"
$env:FORGE_MODEL_BASE_URL = "https://provider.example/v1"
$env:FORGE_MODEL_NAME = "model-id"
$env:FORGE_MODEL_API_KEY = "temporary-secret"
$env:FORGE_AGENT_MAX_COST_USD = "1.00"
.\.venv\Scripts\forge.exe config check
```

Use `anthropic` for Anthropic or `ollama` for a local Ollama service. Ollama normally does not require an API key. Exact pricing is provider-specific; the local budget can only account for usage returned by the provider, so configure a hard spending limit at the provider as well.

The process environment is preferable to a committed configuration file for secrets. ForgeAgent deliberately does not load `.env` files from a target repository.

## 6. Run the safe mode

```powershell
.\.venv\Scripts\forge.exe run "Explain the failing tests and propose the smallest fix" --workspace <workspace>
```

Safe mode does not start an IPython kernel. Repository access is exposed through typed tools with local validation:

- list files;
- search text;
- read bounded file ranges;
- inspect Git status and diff;
- preview and apply exact patches;
- run a narrow set of structured commands.

Command policy has three outcomes:

- `SAFE`: may run locally under the default automatic backend;
- `REVIEW`: requires approval and, in automatic mode, a working Docker sandbox;
- `DENY`: never runs, even if Docker is installed.

The model cannot set its own risk class, approval, or execution backend.

## 7. Understand trusted RLM mode

```powershell
.\.venv\Scripts\forge.exe rlm run "Investigate the repository" --workspace <trusted-workspace> --trust-workspace
```

RLM mode adds a persistent IPython kernel, durable sessions, child agents, parent/child messages, and structured context compaction. It is useful for long analyses and Python-assisted reasoning.

The trust flag is important: Python inside the kernel inherits the current user's permissions and can import `os`, `subprocess`, and file APIs. Host Bridge requests are validated, and common credential variables are removed, but the kernel itself is **not a sandbox**. Use it only with repositories you trust.

Session data is stored under:

```text
<workspace>/.forge/sessions/<root-session-id>/
```

Entries use append-only UTF-8 JSON Lines. If a process stops during a write, ForgeAgent reports the last good byte offset. Repair is explicit so recovery never silently discards data:

```powershell
.\.venv\Scripts\forge.exe session list --workspace <workspace>
.\.venv\Scripts\forge.exe session show <session-id> --workspace <workspace>
.\.venv\Scripts\forge.exe session repair <session-id> --workspace <workspace> --truncate-tail
```

## 8. Project architecture

```text
CLI
  -> configuration and secret validation
  -> provider adapter
  -> runtime / turn engine
  -> context builder
  -> tool registry and local policy
  -> repository, patch, Git, command, or sandbox tool
  -> redacted events and result
```

Trusted RLM extends the same foundation:

```text
RLM application
  -> session runtime and durable queue
  -> append-only session store
  -> persistent kernel and host bridge
  -> child manager and message router
  -> shared usage ledger and context compaction
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for component boundaries and invariants.

## 9. Source tree

| Path | Purpose |
| --- | --- |
| `src/forge_agent/domain/` | provider-neutral data structures |
| `src/forge_agent/models/` | model provider adapters |
| `src/forge_agent/runtime.py` | compatible agent runtime entry |
| `src/forge_agent/engine.py` | single-turn model/tool loop |
| `src/forge_agent/context.py` | context construction and bounds |
| `src/forge_agent/tools/` | repository, patch, Git, and command tools |
| `src/forge_agent/policy.py` | deterministic command risk classification |
| `src/forge_agent/sandbox.py` | Docker command specification and cleanup |
| `src/forge_agent/tracing/` | events, redaction, and JSONL traces |
| `src/forge_agent/session/` | durable queue, store, replay, and repair |
| `src/forge_agent/kernel/` | trusted IPython kernel and Host Bridge |
| `src/forge_agent/children.py` | child-agent lifecycle |
| `src/forge_agent/messages.py` | durable family message routing |
| `src/forge_agent/compaction.py` | structured context summaries |
| `tests/` | unit, integration, and offline end-to-end tests |

## 10. Important data structures

### Messages and tool calls

The provider adapter converts every service response into ForgeAgent's own message and tool-call models. This prevents the runtime from depending on one vendor's response object.

### Events

Events contain stable IDs, session/run IDs, correlation IDs, a generation, a kind, and structured data. Start/end or start/failure pairs make concurrent operations reconstructable from the event stream.

### Usage and cost

Usage records input/output tokens and any known cost. `cost_complete=false` means the available response or price data was insufficient. Unknown cost must never be silently treated as zero.

### Session entries

Every durable entry has an ID, optional parent ID, timestamp, and type. Unknown future entry types are preserved during replay but excluded from model context until supported.

## 11. Safety model

ForgeAgent uses several independent controls:

1. **Workspace boundary:** paths must stay inside the chosen repository.
2. **Sensitive-path rules:** common secrets and metadata are rejected.
3. **Exact patches:** edits identify old and new text and can be reviewed as a diff.
4. **Structured commands:** executable and arguments are separate; model-authored shell source is rejected.
5. **Deterministic policy:** local code, not the model, classifies risk.
6. **Approval:** risky but supportable operations require a human decision.
7. **Sandbox routing:** approved review commands require Docker in automatic mode.
8. **Redaction:** events and errors are filtered before persistence or display.
9. **Budgets:** steps, tools, time, tokens, and known cost have explicit bounds.
10. **Recovery rules:** uncertain external side effects are recorded and not replayed automatically.

No single control makes arbitrary code safe. See [SECURITY.md](SECURITY.md) and [docs/DOCKER_SETUP.md](docs/DOCKER_SETUP.md).

## 12. Run the development checks

```powershell
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest --cov=forge_agent --cov-report=term-missing --cov-fail-under=80 -q
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

Tests that use a Faux provider prove runtime behavior, not model quality. Tests that use a fake sandbox prove routing and command construction, not host isolation. A real claim needs a real provider or Docker host and recorded evidence.

## 13. A good first contribution

1. Pick one issue with a narrow acceptance criterion.
2. Locate the module and adjacent tests.
3. Write a failing test for the desired behavior.
4. Implement the smallest change.
5. Run the focused test, then the full checks.
6. Update user-facing documentation if configuration, output, or safety semantics changed.

Avoid changing provider models, persistence schemas, and authorization policy in one patch. Small changes are easier to review and safer to revert.

## 14. Common problems

### `forge doctor` reports a missing model

Set the provider, base URL, model name, and API key, or run configuration checks with `--allow-missing-model` when you only need offline development.

### A reviewed command returns `sandbox_unavailable`

This is expected when automatic routing requires Docker but Docker, its daemon, or the configured image is unavailable. ForgeAgent fails closed rather than running the command locally.

### A kernel command is refused

RLM commands require `--trust-workspace`. Confirm the repository is trusted; do not add the flag merely to bypass the warning.

### Cost is incomplete

The provider may not have returned usage, or no explicit pricing is configured. Treat the reported cost as a lower bound and rely on a provider-side hard limit.

### A session has a truncated last line

Inspect it first, then use the explicit repair command. Never edit an active session file with a text editor.

## 15. Suggested learning order

1. Python functions, classes, exceptions, type hints, and `async`/`await`.
2. Pydantic models and validation.
3. HTTP requests and streaming responses.
4. subprocess lifecycle and cancellation.
5. JSON Lines persistence and file synchronization.
6. ForgeAgent domain models and Faux provider tests.
7. Runtime, tools, and policy.
8. Durable sessions, kernels, children, and compaction.

You do not need to understand the entire system before contributing. Start at one boundary, use tests as executable examples, and preserve the documented safety invariants.
