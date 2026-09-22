# Target repository dependencies

The generic `forge-agent:dev` image contains ForgeAgent, pytest, Ruff and mypy. It deliberately does not install arbitrary dependencies from a target repository at runtime.

For a trusted target:

1. Export a reviewed, pinned `requirements.lock`.
2. On a network-enabled preparation machine, download wheels for the exact container platform:

   ```powershell
   python -m pip download --requirement requirements.lock --dest wheels
   ```

3. Scan and review `requirements.lock` and `wheels/`.
4. Build a derived image using `target.Dockerfile.example` as the template.
5. Set `FORGE_DOCKER_IMAGE` to the derived image.

The runtime container remains offline and installs nothing. Missing executables or imports should fail normally and be reported through `run_command`; do not solve them by enabling runtime networking or mounting a host virtual environment.

This process is intentionally language-specific. Node, Rust, Java and other ecosystems need equivalent locked, offline artifact preparation before they are added to the command allowlist.
