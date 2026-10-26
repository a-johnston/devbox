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
devbox attach [id]                                # attach, recovering an exited harness in place
devbox shell [id]                                 # non-root bash shell
devbox rm [id ...]                                # remove container, keep history and clone
devbox fetch [id]                                 # fetch new work as local <instance id>/<branch>
devbox review [id] [branch]                       # fetch, pick a branch, and show its diff
devbox land [id] [branch]                         # fetch, rebase onto the current host branch, ff-merge
devbox sessions                                   # saved conversations, including removed containers
devbox sync [id] [branch ...]                     # deliver host branches to an instance as host/<branch>
devbox put id path ...                            # copy host files into the instance's /devbox/inbox
devbox get id [path ...]                          # copy files from its /devbox/outbox (or any path)
devbox fork [session-id]                          # fork a conversation into a new container
devbox build pi                                   # rebuild the project/runtime/harness image
devbox cleanup [--only {instances,images}]        # remove orphaned state and unused images
devbox init                                       # default template .devbox.toml and .devbox/Containerfile
devbox completion {bash,zsh,fish} | source        # install completions 
devbox [command] --help                           # full options
```

New instances run the selected harness in tmux within the container - `C+a` then `d` detaches without stopping the harness.
Instance IDs accept unique shorthand, e.g. `g` instead of `devbox-green`.

### Attach, fork and container lifecycle

`devbox attach [id]` reconnects to a live harness. If the harness has exited or tmux was killed,
attach recreates it **in the same running container**, continuing that instance's most recently
modified valid conversation. If there is no saved conversation, it starts a fresh one. The
repository, uncommitted files, home and container filesystem stay in place. Recovery is announced,
uses the instance's saved launch policy (including `--no-config`), and reacquires declared
credentials from the host; their values are never saved. It does not rebuild images or copy config
again. A continuation failure is an error, not a silent switch to an empty conversation.

`devbox fork [session-id]` (formerly `resume`) always creates a **new instance**, leaving the source
untouched, whether its container is running, stopped or removed. It copies conversation history
and clones the source instance's committed repository state by default. Use `--from host` to clone
the host checkout instead, or `--from instance` to require the saved clone. This is not a container
snapshot: uncommitted files and the old container's writable layer are not copied.

Devbox does not start or stop existing containers. Use the selected runtime's CLI, e.g.
`container stop devbox-green` / `container start devbox-green`, or the corresponding `podman`
commands. Once a stopped container is started, `devbox attach` can recover its harness. `devbox rm`
remains the normal disposal command and retains saved conversations and the repository clone.
`ls` includes stopped containers; `shell`, `sync`, `put` and `get` need running ones. Saved
conversations and clones remain usable by `sessions`, `fork`, `fetch`, `review` and `land` after
container removal. `cleanup` only offers saved instances whose containers no longer exist, not
ones that are merely stopped.

Instances created before launch policy was saved can still attach to an existing tmux session,
but cannot automatically recover an exited harness; use `fork` or inspect them with `shell`.

## Project setup

`devbox init` writes a template `.devbox.toml` and minimal `.devbox/Containerfile` at the project root if the files don't
already exist. The templates can be found [here](src/devbox/assets/example) (see also this repo's [config](.devbox.toml) and [containerfile](.devbox/Containerfile)).

The project Containerfile installs dependencies on a Debian/Ubuntu (apt, glibc) base; devbox adds the terminal
runtime and harness. The image context and Containerfile must be inside the project and outside `.git`.

## Creating Instances

`devbox new` creates a new container with the selected harness for a given project path, defaulting to the current
directory. Each instance clones the committed `HEAD` of the active checkout into its saved state, and mounts the clone
at `/workspace/<name>`, named after the primary checkout even when started from a worktree. Uncommitted changes are not copied. The clone has no remote and must be resynced with the host
checkout with `devbox sync`. Agents learn these constraints from devbox's [instructions](src/devbox/assets/runtime/environment.md),
which every harness appends to its system prompt. Container images are reused when possible and only require rebuilding
when the project dependencies or harness version change.

The built-in Claude harness runs with `--dangerously-skip-permissions` (YOLO mode) for both new and
forked and recovered sessions, including with `--no-config`. Claude does not ask for tool permission approvals;
the devbox container is the isolation boundary. Only run trusted tasks: Claude can still modify
its clone, saved conversations and any explicitly shared harness state.

## Fetch and Review

Containers create local branches and `devbox fetch` copies them into the host repo. Container
branches with commits not yet in the current host `HEAD` can then be referenced as local
branches named `<id>/<branch>` (eg `orange/my-feature` from `devbox-orange`). Commit counts
exclude work already in the host branch. Warnings about changes to the config, Containerfile,
build context, and `[review] watch` paths use each branch's merge base with the current host
`HEAD`, not the instance's original starting commit.

`devbox review [id] [branch]` fetches, picks a branch, and shows `git diff HEAD...<fetched-ref>`:
only that branch's changes since its most recent shared ancestor with the host branch.
Review and land use the **primary host checkout's current branch**, even if the instance
started from another branch or worktree. Without an explicit branch, their picker omits
branches with no new commits. After review, `devbox land [id] [branch]` locally rebases
`<short-instance>/<branch>` onto that current host branch and then fast-forward merges.
After a successful merge it deletes that local branch, without forcing deletion. The
instance's branch and the fetched tracking ref are retained; failed rebases or merges
leave the local branch intact.

## Sync

`devbox sync [id] [branch ...]` delivers host branches to a running instance as
`host/<branch>` refs (default: the branch it started from), then keeps the clone's local
branches in step where that loses nothing. Inside the guest, a local branch that is a strict
ancestor of `host/<branch>` is fast-forwarded. The checked-out branch moves only when
tracked files have no uncommitted changes and no rebase, merge or similar is in progress;
a branch checked out in another worktree, with extra commits or diverged is left alone, and
`host/<branch>` remains for the agent to use. Each branch gets one report line.

`devbox land` runs the same sync after a successful merge, for the landed target branch in
every running instance of the project (stopped ones are reported, not synced). The landing
instance's own branch is also moved to its rebased commits when it is still at the fetched
tip, so later fetches and branches stacked on it see the commits that were landed. A failed
sync is reported and never undoes the landing.

## Files

`devbox put <id> <path> ...` copies host files and directories into a running instance's
`/devbox/inbox/`, or a directory within it given by `--dir`. Each one is named after its own
basename, whatever directory it came from, so `~/Downloads/crash.log` arrives as
`/devbox/inbox/crash.log`. A symlink argument sends what it points to; links inside directories
stay links. Quote a glob to let devbox expand it, including `**`.

`devbox get <id> [<path> ...]` copies files the other way, into the current directory or `--to`.
Relative paths and quoted globs are resolved within `/devbox/outbox/`, where agents are told to
leave documents, images and other files that don't belong in the repository; absolute paths
can name anything in the guest. Without paths it offers the outbox in a picker, and shell
completion lists the instance's files. Received archives extract only the requested names and
refuse absolute or escaping links.

Neither command replaces an existing name without `--force`, and both check every name before
copying anything. The inbox and outbox live on the container's disk: they aren't shared with
other instances, and removing the container removes them.

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

Claude devbox instances likewise share a devbox-only login in `$XDG_CONFIG_HOME/devbox/shared/claude/auth/`
(eg `~/.config/devbox/shared/claude/auth/`). Host Claude auth (the macOS Keychain) is never used: run
`/login` in any instance once and later instances, forked ones included, reuse it, while settings and
conversations stay per instance. `/logout` affects every instance. Claude selects this store with the
undocumented `CLAUDE_SECURESTORAGE_CONFIG_DIR`, so a Claude update could break the sharing. Refreshes are
coordinated with Claude's own lock directories in the shared mount. `--no-config` instances keep a
private login in their own home instead.

Alternatively, create a separate long-lived token with `claude setup-token`. When set, it takes
precedence over the shared login. Supply it via `CLAUDE_CODE_OAUTH_TOKEN` in devbox's environment, or save it
in `$XDG_CONFIG_HOME/devbox/config.toml` (default `~/.config/devbox/config.toml`):

```toml
[secrets]
CLAUDE_CODE_OAUTH_TOKEN = "your-token"
```

A nonempty environment value takes precedence over the config file. Only secrets declared by the
selected harness are forwarded, and only to the harness process - not to images, container config or
saved state. `--no-config` skips both sources.

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

## Development

Targets python >= 3.12

```sh
uv sync --locked
uv run pytest
uv run prek install
uv run prek run --all-files
```

- [adding a container engine](src/devbox/engine/README.md)
- [adding a harness](src/devbox/assets/harnesses/README.md)
