# Evaluation and experiment reports

This directory contains historical measurements and reproducible offline evidence. The reports are not interchangeable: a deterministic reference harness, a synthetic context-size proxy, and a live model run support different claims.

## Existing reports

- [`evaluation-reference/evaluation.md`](evaluation-reference/evaluation.md) is a historical report for version `0.1.0` at commit `3b5ebd5`. Its 60/60 result came from the deterministic reference harness, not a language model. It is not a current ForgeAgent model benchmark or a claim about autonomous coding quality.
- [`context_ablation.md`](context_ablation.md) records a synthetic experiment dated 2026-07-26. It uses a simple character-to-token proxy and deterministic generated tasks; it does not call a model or measure task success. Reproduce its JSON output from the repository root with:

  ```powershell
  python experiments/context_ablation.py
  ```

- [`provider_compatibility.md`](provider_compatibility.md) summarizes protocol behavior covered by offline tests. Mocked HTTP tests do not verify live services.

## Running the reference harness

From the repository root, run the current CLI reference mode and direct generated files into the ignored local runtime directory:

```powershell
forge eval run --mode reference --repetitions 1 --output .forge/reports/reference
```

On Windows PowerShell in the repository's virtual environment, use `.\.venv\Scripts\forge.exe` in place of `forge`. The command runs deterministic reference tasks; it does not call a model. It writes `evaluation.json` and `evaluation.md` beneath `.forge/reports/reference` rather than the tracked `reports/` directory. The repository `.gitignore` excludes `.forge/`; still check `git status` before publishing and never include private traces, task repositories, credentials, or session data in a report.

For a live evaluation, choose a provider, set a provider-side spending limit, and review the generated report for private repository information before sharing it. Live evaluation can incur charges and can contain task-derived data; do not publish its output without checking it.
