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
Run `uv run pytest` after every source change, and run all prek hooks before requesting landing approval.

## Development workflow

Make project changes on a new branch in an in-repo worktree, created before the first edit:

```sh
git worktree add .worktrees/<name> -b <branch>
cd .worktrees/<name>
uv sync --locked
```

Do not edit or commit feature work directly on `main`, even for small changes. Keep unrelated work
and existing uncommitted changes intact. Directly requested procedural/meta changes (such as edits
to this file) may be made in the primary checkout. Do not delegate to other agents unless asked.

Prefer cohesive commits: amend or squash related fixups and review iterations, while keeping
independent changes separate. When finished, stop and ask for approval before landing on `main`.
If the branch has more than one commit beyond its base, ask whether to squash together with the
landing approval request, unless the user already specified the desired history. Do not ask about
squashing for a branch with at most one commit beyond its base.

After approval, rebase onto the current `main`, apply any agreed squashing, and rerun
`uv run prek run --all-files`. Land with `git merge --ff-only <branch>` from the primary checkout;
never overwrite unrelated local changes. Remove the worktree and delete its branch only after
confirming the changes are on `main`. Remove Agentbox containers mounting that worktree before
removing or renaming it.

Keep the host wrapper independent of particular projects and harnesses. Project manifests own workspace mounts, generated directories and build dependencies. Harness profiles own installation, selected configuration and session conventions. Never mount the host home or expose a host command/socket bridge by default. Never use container-wide `--all` cleanup flags: filter our ownership labels first. Container removal must not delete saved conversations. Guest runtime helpers run under Linux; tests must not require a running container service unless explicitly invoked as integration checks.
