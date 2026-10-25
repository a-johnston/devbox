# devbox

Run Pi or Claude Code in disposable Linux environments with a clone of the target project.

## Install

- Recommended [uv](https://docs.astral.sh/uv/) for installation and development
- Optionally [fzf](https://github.com/junegunn/fzf) for interactive commands
- Container engine
  - macos: [container](https://github.com/apple/container)
  - linux: [podman](https://wiki.archlinux.org/title/Podman) (rootless, shared kernel, https://github.com/containers/crun/issues/2090)

Clone this repo and install with:

```sh
uv tool install --editable .
```

## Use

```sh
devbox new [{pi,claude}] [--project project]      # create a new devbox container. project defaults to .
devbox review [id] [branch]                       # fetch, pick a branch, and show its diff
devbox land [id] [branch]                         # fetch, rebase onto the current host branch, ff-merge

devbox fetch [id]                                 # fetch new work as local <instance id>/<branch>
devbox sync [id] [branch ...]                     # deliver host branches to an instance as host/<branch>
devbox put id path ...                            # copy host files into the instance's /devbox/inbox
devbox get id [path ...]                          # copy files from its /devbox/outbox (or any path)
devbox attach [id]                                # attach to an existing instance's harness, recreating it if killed
devbox shell [id]                                 # attach to an existing instance in a non-root bash shell

devbox ls                                         # list running and stopped managed containers
devbox sessions                                   # list saved conversations, including removed containers
devbox fork [session-id]                          # fork a conversation into a new container
devbox rm [id ...]                                # remove container, keeping history and clone
devbox rm-all                                     # remove all managed containers
devbox cleanup [--only {instances,images}]        # remove orphaned state and unused images

devbox init                                       # default template .devbox.toml and .devbox/Containerfile
devbox build [harness]                            # rebuild the project/runtime/harness image
devbox completion {bash,zsh,fish} | source        # install completions 
```

New instances run the selected harness in tmux within the container - `C+a` then `d` detaches without stopping the harness.
Instance IDs accept unique shorthand, e.g. `g` instead of `devbox-green`.

## Project setup

`devbox init` writes a template `.devbox.toml` and minimal `.devbox/Containerfile` at the project root if the files don't
already exist. The templates can be found [here](src/devbox/assets/example) (see also this repo's [config](.devbox.toml)
and [containerfile](.devbox/Containerfile)).

The project Containerfile installs dependencies on a Debian/Ubuntu (apt, glibc) base; devbox adds the terminal
runtime and harness on its own.

`devbox fetch` always warns about branches that change the project config, Containerfile or build context. To flag other
paths, such as hooks or scripts that run on the host, list them as paths relative to the repository root under `[review] watch`:

```toml
[review]
watch = ["prek.toml", "scripts/**"]
```

This check always uses the host's config rather than the fetched config. Supports glob patterns.

## Creating Instances

`devbox new` creates a new container with the selected harness for a given project path, defaulting to the current
directory. Each instance clones the committed `HEAD` of the active checkout into its saved state, and mounts the clone
at `/workspace/<name>`. Uncommitted changes are not copied. The clone has no remote and must be resynced with the host
checkout with `devbox sync`. Agents learn these constraints from devbox's [instructions](src/devbox/assets/runtime/environment.md),
which every harness appends to its system prompt. Container images are reused when possible and only require rebuilding
when the project dependencies or harness version change.

## Fetch, Review, and Land

Containers create local branches and `devbox fetch` copies them into the host repo. Container
branches with commits not yet in the current host `HEAD` can then be referenced as local
branches named `<id>/<branch>` (eg `orange/my-feature` from `devbox-orange`). Commit counts
exclude work already in the host branch. Warnings are logged for changes to the config, Containerfile,
build context, and configured `[review] watch` paths compared to each branch's merge base.

`devbox review [id] [branch]` fetches, picks a branch, and shows `git diff HEAD...<fetched-ref>`:
only that branch's changes since its most recent shared ancestor with the host branch.
Review and land use the **host checkout's current branch**, even if the instance started
from another branch or worktree. Without an explicit branch, their picker omits branches
with no new commits.

After reviewing a change, `devbox land [id] [branch]` locally rebases `<short-instance>/<branch>`
onto the host's current branch and then fast-forward merges. After a successful merge, the
local fetched branch is deleted. Similar to review, branch can be omitted if there's only one
branch with new commits available from a given instance.

## Sync

`devbox sync [id] [branch ...]` delivers host branches to a running instance as `host/<branch>`
refs and fast-forwards branches when possible. If no branch is passed, the branch the instance
was started with is synced. Each moved branch is reported with its old tip, and the report is
also left in the instance's `~/.devbox/sync.txt` for the agent. `--force` also replaces local
branches that are diverged from or ahead of the host's (for example to undo a mistaken landing
in the clones); uncommitted changes and in-progress Git operations are still never overridden,
and replaced tips remain in the reflog.

By default, `devbox land` automatically syncs the target branch with the landed changes in
every running instance of the project. In a terminal it asks first, because a mistaken landing
is easy to fix on the host but not once clones have it; declining leaves the landing in place.
`--sync` skips the question and `--no-sync` skips the sync.

## Files

`devbox put <id> <path> ...` copies host files and directories into a running instance's
`/devbox/inbox/`, or a directory within it given by `--dir`. Each one is named after its own
basename regardless of its directory, so `~/Downloads/crash.log` becomes `/devbox/inbox/crash.log`.
Follows symlinks and supports glob patterns.

`devbox get <id> [<path> ...]` copies files the other way, into the current directory or `--to`.
Relative paths and quoted globs are resolved from `/devbox/outbox/`. Absolute paths can name anything
in the container.

Neither command replaces an existing path without `--force`, and both check every name before
copying anything. The inbox and outbox live on the container's disk: they aren't shared with
other instances, and removing the container removes them.

## Config and Credentials

Each instance gets an isolated copy of selected harness settings. Pi installs portable npm/git package
declarations from host settings. Host-specific settings, local packages and Claude hooks/plugins are omitted.
Use `--no-config` to skip importing settings/packages/credentials and mounting shared harness state, then
log in independently inside the guest.

Both Pi and Claude instances support a shared login between containers using the same harness. For each harness,
a path under `~/.config/devbox/shared/` is mounted to each container for that harness such that tokens and refreshes
can be coordinated between containers. The host login is never used or copied, so the first instance for a given
container will need to run `/login` and get a new login auth. Pi uses a [custom wrapper](src/devbox/assets/harnesses/pi/README.md)
to redirect the auth path, Claude relies on an undocumented option `CLAUDE_SECURESTORAGE_CONFIG_DIR` to do the same.

Alternatively, Claude instances can use a separate long-lived token created with `claude setup-token` passed either
via `CLAUDE_CODE_OAUTH_TOKEN` or saved in `$XDG_CONFIG_HOME/devbox/config.toml` (default `~/.config/devbox/config.toml`):

```toml
[secrets]
CLAUDE_CODE_OAUTH_TOKEN = "your-token"
```

A nonempty environment value takes precedence over the config file and both take precedence over the shared login.
Only secrets declared by the selected harness are forwarded, and only to the harness process - not to images, container
config or saved state. Instances run with `--no-config` do not use any of these three auth sources.

Git author identity, `COLORTERM` and, for `attach` and `shell`, `TERM` are inherited automatically.
Other environment values must be declared by the project or harness. Harness profiles live in
`$XDG_CONFIG_HOME/devbox/harnesses/<name>/`; see [harness docs](src/devbox/assets/harnesses/README.md).

## Saved state

Container state by default is saved to `$XDG_DATA_HOME/devbox/{apple,podman}/instances/<id>/`
(default `~/.local/share/devbox`) unless overridden with `--state-dir`. Removing a container does not remove
the saved state.

`devbox cleanup` previews orphaned saved instances and/or unused devbox images, optionally `--select`ing
which ones to clean up, then asks for confirmation before deleting that data, which includes conversations
and unfetched branches.

## Attach, Fork and Container Lifecycle

`devbox attach [id]` reconnects to a live harness. If the harness has exited or tmux was killed, attach recreates it in the
same running container, resuming that instance's most recently modified conversation if one is present.

`devbox fork [session-id]` forks an existing session into a new session and new container. Conversation history and project
state are copied to the new instance from the source session by default. Use `--from host` to clone the host checkout instead.
This is not a container snapshot: uncommitted files and the old container's writable layer are not copied.

## Development

Targets python >= 3.12

```sh
uv sync --locked
uv run pytest
uv run prek install
uv run prek run --all-files
```

- [container engine docs](src/devbox/engine/README.md)
- [harness docs](src/devbox/assets/harnesses/README.md)
