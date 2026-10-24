# devbox

Run Pi or Claude Code in disposable Linux environments with a clone of the target project.

## Install

- Recommended [uv](https://docs.astral.sh/uv/) for installation and development
- Optionally [fzf](https://github.com/junegunn/fzf) for interactive commands
- Container engine
  - macos: [container](https://github.com/apple/container)
  - linux: [podman](https://wiki.archlinux.org/title/Podman) (rootless, shared kernel, https://github.com/containers/crun/issues/2090)

Clone this repo and install:

```sh
uv tool install --editable .
```

## Use

```sh
devbox new [{pi,claude}] [--project project]      # project defaults to .
devbox ls                                         # list running and stopped managed containers
devbox attach [id]                                # attach to a container. id can be a unique shorthand eg "g" instead of devbox-green
devbox shell [id]                                 # non-root bash shell
devbox stop [id ...]                              # stop processes, keep container disk
devbox rm [id ...]                                # remove container, keep history and clone
devbox fetch [id]                                 # fetch new work as local <instance id>/<branch>
devbox review [id] [branch]                       # fetch, pick a branch, and show its diff
devbox land [id] [branch]                         # fetch, rebase onto the current host branch, ff-merge
devbox sessions                                   # saved conversations, including removed containers
devbox sync [id] [branch ...]                     # deliver host branches to an instance as host/<branch>
devbox resume [session-id]                        # fork a conversation into a new container
devbox build pi                                   # rebuild the project/runtime/harness image
devbox cleanup [--only {instances,images}]        # remove orphaned state and unused images
devbox init                                       # default template .devbox.toml and .devbox/Containerfile
devbox completion {bash,zsh,fish} | source        # install completions 
devbox [command] --help                           # full options
```

New instances run the selected harness in tmux within the container - `C+a` then `d` detaches without stopping the harness.

## Project setup

`devbox init` writes a template `.devbox.toml` and minimal `.devbox/Containerfile` at the project root if the files don't
already exist. The templates can be found [here](src/devbox/assets/example) (see also this repo's [config](.devbox.toml) and [containerfile](.devbox/Containerfile)).

The project Containerfile installs dependencies on a Debian/Ubuntu (apt, glibc) base; devbox adds the terminal
runtime and harness. The image context and Containerfile must be inside the project and outside `.git`.

## Creating Instances

`devbox new` creates a new container with the selected harness for a given project path, defaulting to the current
directory. Each instance clones the committed `HEAD` of the active checkout into its saved state, and mounts the clone
at the checkout's path. Uncommitted changes are not copied. The clone has no remote and must be resynced with the host
checkout with `devbox sync`. Agents learn these constraints from devbox's [instructions](src/devbox/assets/runtime/environment.md),
which every harness appends to its system prompt. Container images are reused when possible and only require rebuilding
when the project dependencies or harness version change.

## Fetch and Review

Containers create local branches and `devbox fetch` copies them into the host repo. Container
branches with new commits can then be referenced as local branches named `<id>/<branch>` (eg
`orange/my-feature` from `devbox-orange`). Warns about any changes to the config, Containerfile,
build context, and `[review] watch` paths.

`devbox review [id] [branch]` fetches, picks a branch, and shows its changes vs the base with
`git diff`. After review, `devbox land [id] [branch]` locally rebases `<short-instance>/<branch>`
onto the current branch and then fast-forward merges. After a successful merge it deletes
that local `<short-instance>/<branch>` branch, without forcing deletion. The instance's
branch and the fetched tracking ref are retained; failed rebases or merges leave the local
branch intact.

## Sync

`devbox sync [id] [branch ...]` delivers host branches to a running instance as
`host/<branch>` refs (default: the branch it started from), so the agent can rebase onto
newer work.

## Config and credentials

Each instance gets an isolated copy of selected harness settings; changes never sync back.
Pi installs portable npm/git package declarations from host settings. Host-specific settings,
local packages and Claude hooks/plugins are omitted. Use `--no-config` to skip importing
settings/packages/credentials and mounting shared harness state, then log in independently
inside the guest.

Pi devbox instances uses a custom auth path at `$XDG_CONFIG_HOME/devbox/shared/pi/auth/`
(eg `~/.config/devbox/shared/pi/auth/`) which they share in order to coordinate token refreshes
across instances. The host auth is never used, so the first pi instance will need to `/login`.
Doesn't apply to instances created with `--no-config`. This is done with a small custom launcher;
see [pi/README.md](src/devbox/assets/harnesses/pi/README.md).

Claude's host login is also not used: use `/login` in the guest, or create a separate token
with `claude setup-token` and supply `CLAUDE_CODE_OAUTH_TOKEN` to devbox.

Git author identity, `COLORTERM` and, for `attach` and `shell`, `TERM` are inherited automatically.
Other environment values must be declared by the project or harness. Harness profiles live in
`$XDG_CONFIG_HOME/devbox/harnesses/<name>/`; see [harness docs](src/devbox/assets/harnesses/README.md).

## Saved state

Container state by default is saved to `$XDG_DATA_HOME/devbox/{apple,podman}/instances/<id>/`
(eg `~/.local/share/devbox`) unless overridden with `--state-dir`. Removing a container does not remove
the saved state.

`devbox cleanup` previews orphaned saved instances and/or unused devbox images, optionally `--select`ing
which ones to clean up, then asks for confirmation before deleting that data, which includes conversations
and unfetched branches.

## Development

Targets python >= 3.12

```sh
uv sync --locked
uv run prek install
uv run pytest                        # no running container engine needed
uv run prek run --all-files           # lint, format and types
```

Tests run explicitly, not in prek: the full suite exercises real Git clones, worktrees,
fetches and rebases, whose subprocess overhead makes it unsuitable for a per-commit hook.

- [adding a container engine](src/devbox/engine/README.md)
