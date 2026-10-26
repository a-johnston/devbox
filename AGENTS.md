## Design

This project is designed to layer project-specific container setup with devbox-managed coding harnesses and
these layers must remain independent. Project manifests own workspace mounts, generated directories and build
dependencies. Harness profiles own installation, selected configuration and session conventions.

## Code and test design

Give each behavior one owner. The layer that decides an operation performs all of it: `App` methods
carry out whole operations (landing includes syncing instances), while the CLI selects inputs and
prints results. Do not add a public method just so a caller can sequence a step that belongs to the
same operation, or move behavior into the CLI to keep printing there; return the results instead.

Test each unit through its inputs and outputs: `repository` functions against real repositories,
guest scripts by running them directly, `App` methods with `FakeEngine`/`FakeGit` and stubs for the
units they call, and CLI handlers with stubbed `App` results. A test of a composition checks that it
calls its collaborators with the right arguments and handles their results and failures; it does
not re-exercise behavior their own tests cover. Keep end-to-end tests few and deliberate: per
workflow, one base case or one compound case that exercises the real pieces together, chosen to
minimize setup and subprocesses. Prefer copies of the session template repository over creating
repositories or instances with real Git.

## Tooling

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

These instructions apply on the host. Inside a devbox container (`DEVBOX_INSTANCE` is set) the
checkout is a private clone: make changes on a new branch there instead of a worktree, commit,
and ask the user to review it with `devbox review <instance>`. Landing, `devbox` commands and
worktree cleanup happen on the host. `devbox sync` and `devbox land` may update the clone's
branches (fast-forwarding, or rewriting a landed branch to its rebased commits) when the tree
is clean, so check `git status` and `host/*` before relying on branch state.

On the host, make project changes on a new branch in an in-repo worktree, created before the first edit:

```sh
git worktree add .worktrees/<name> -b <branch>
cd .worktrees/<name>
uv sync --locked
```

When dependencies change, update `uv.lock` alongside `pyproject.toml` and run `uv sync --locked`
in the affected checkout/worktree. After landing, also sync the primary checkout's `.venv`.
An editable install picks up source edits, but does not automatically install new dependencies:
if runtime dependencies change and devbox is installed with `uv tool`, refresh that separate
managed environment with `uv tool install --editable . --force` from the primary checkout.
Verify the installed tool's executable as well as `uv run` commands; they use separate environments.

Do not edit or commit feature work directly on `main`, even for small changes. Keep unrelated work
and existing uncommitted changes intact.

Prefer cohesive commits: amend or squash related fixups and review iterations, while keeping
independent changes separate. When finished, stop and ask for approval before landing on `main`.

After approval, rebase onto the current `main` and rerun `uv run prek run --all-files`. Land
with `git merge --ff-only <branch>` from the primary checkout; never overwrite unrelated local changes.
Remove the worktree and delete its branch only after confirming the changes are on `main`.
