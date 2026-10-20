# Agentbox

Python glue for Apple's `container` CLI. Development uses Python 3.15; maintain Python 3.12 compatibility.

Use project-local tooling through uv:

```sh
uv sync --locked
uv run ruff check . --fix
uv run ruff format .
uv run ty check .
uv run pytest
uv run prek run --all-files
```

`prek.toml` uses local/system hooks that explicitly invoke `uv run`, not isolated hook environments.

Keep the host wrapper independent of particular projects and harnesses. Project manifests own workspace mounts, generated directories and build dependencies. Harness profiles own installation, selected configuration and session conventions. Never mount the host home or expose a host command/socket bridge by default. Never use container-wide `--all` cleanup flags: filter our ownership labels first. Container removal must not delete saved conversations. Guest runtime helpers run under Linux; tests must not require a running container service unless explicitly invoked as integration checks.
