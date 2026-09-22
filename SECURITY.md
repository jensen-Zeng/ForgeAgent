# Security policy

ForgeAgent can modify files and execute allowlisted commands. Use it only in repositories that are backed up or committed and review every Diff. The default `auto` mode keeps `SAFE` commands local, routes approved `REVIEW` commands to Docker, and rejects `DENY` commands before backend selection.

## Supported version

Security fixes currently target the latest `0.3.0.dev0` source on `main`. Pre-release versions do not yet receive long-term support.

## Reporting a vulnerability

Do not publish credentials, exploit payloads or private repository contents in a public issue. Use a [private GitHub security advisory](https://github.com/jensen-Zeng/ForgeAgent/security/advisories/new). Include the affected version, operating system, minimal reproduction, expected boundary and observed impact.

## Security boundaries

- Filesystem tools reject paths outside the selected workspace and sensitive paths.
- Patches use exact context and do not expose arbitrary overwrite or deletion.
- Commands are structured, shell-free, classified by a narrow policy and run with a sanitized environment.
- Automatic routing is decided by local policy, not model arguments. Docker failure never falls back to local execution.
- Docker isolation must be validated on the actual host and container runtime before it is treated as a security boundary.
- A container reduces risk but is not a virtual machine or a guarantee against kernel/container-runtime vulnerabilities.
- Local cost limits are not substitutes for provider-side billing limits.

## Trusted RLM mode

`forge rlm run` and `forge rlm resume` refuse to start unless the user supplies `--trust-workspace`. This acknowledgement is required because the persistent IPython Kernel inherits the current user's permissions. It can import `os`, `subprocess`, open files, start processes and bypass ForgeAgent's normal tool policy. It is an execution feature for repositories the user has already chosen to trust, not a sandbox.

The host removes common credential variables from the Kernel environment and does not inject API keys, Provider clients, SessionStore handles or child-management objects. Auditable operations use validated Host Bridge requests whose sender identity is bound to the owning Kernel. `rlm.shell()` uses a structured executable/argument request, bounded cwd, UTF-8 decoding, timeout, output artifact and process-tree cleanup. These controls improve auditability but do not make arbitrary Kernel Python safe.

RLM state is stored as append-only UTF-8 JSONL under `.forge/sessions`. Inputs are flushed and fsynced before acknowledgement. Recovery does not automatically replay an input that had started before interruption, because its external side effects may be uncertain. Tail truncation is only performed by the explicit `forge session repair ... --truncate-tail` command.

See [docs/DOCKER_SETUP.md](./docs/DOCKER_SETUP.md) and [docs/FAILURE_CASES.md](./docs/FAILURE_CASES.md) for current limitations.
