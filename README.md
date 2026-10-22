# devbox

Run Pi or Claude Code in disposable Linux environments: a VM per container with
[Apple's `container`](https://github.com/apple/container) on macOS, or rootless
[Podman](https://podman.io) containers on Linux. Your checkout is writable at its original
path; the rest of your home stays on the host. Conversations survive container removal.

## Install

Requires [uv](https://docs.astral.sh/uv/) and a container engine:

- **macOS, Apple silicon:** Apple's `container` **1.5+**. Start it with
  `container system start`; Devbox also offers to do this interactively.
- **Linux, x86_64 or arm64:** rootless Podman **4.3+**, cgroup v2, and user entries in
  `/etc/subuid` and `/etc/subgid`. Check your setup with `podman info`. CPU/memory limits
  require cgroup delegation; rootless cgroupfs does not enforce them.

Install from this checkout:

```sh
uv tool install --editable .
```

Install [fzf](https://github.com/junegunn/fzf) for interactive pickers; explicit arguments
work without it. The engine is selected by OS: Apple on macOS, Podman on Linux.

## Use

```sh
devbox new pi                         # project defaults to .; omit harness for a picker
devbox new claude --detach
devbox ls                             # running and stopped managed containers
devbox attach [id]                    # omit ID for a picker; unique prefixes work
devbox shell [id]                     # non-root inspection shell
devbox stop [id ...]                  # stop processes, keep container disk
devbox rm [id ...]                    # remove container and artifact volumes, keep history
devbox sessions                       # saved conversations, including removed containers
devbox resume [session-id]            # fork a conversation into a new container
devbox build pi                       # rebuild the project/runtime/harness image
devbox cleanup                       # confirm deletion of orphaned state and unused images
```

`stop` and `rm` accept multiple IDs or unique prefixes; omit IDs for a multi-select picker.

**Ctrl-a, then d** detaches without stopping the harness. After `stop`, use `resume` to
continue a conversation. `kill-all` removes all managed containers but keeps saved history.
See `devbox --help` and `<command> --help` for options, including JSON output and shell completion.

## Project setup

Add `.devbox.toml` (see [this repo's example](.devbox.toml)):

```toml
version = 1

[image]
containerfile = ".devbox/Containerfile"
context = ".devbox"                  # keep source and .git out of the build context

[container]
cpus = 4
memory = "8G"

[workspace]
artifacts = [".worktrees", "build"]   # guest-local volumes hide these host directories

[environment]
MY_SETTING = "value"
```

The project Containerfile installs dependencies on a Debian/Ubuntu (apt, glibc) base;
Devbox adds the terminal runtime and harness. Manifests are discovered upwards from
`--project`, with a fallback to the primary checkout for in-repo Git worktrees. The whole
primary checkout is mounted.

Artifact volumes survive `stop` but are deleted by `rm`. Tracked files under artifact paths
are restored into the volumes. If `.worktrees` is an artifact, launch from the primary
checkout and create worktrees inside the guest. **Commit before removing the container:**
uncommitted files in those worktrees are lost. Run `git worktree prune` afterwards.

The first launch builds an image; subsequent launches reuse it. Run `devbox build <harness>`
after changing Containerfiles, build contexts or Devbox. Harness versions follow the host
installation when available, otherwise a pinned default. On macOS, builder memory defaults
to `4G`; override with `DEVBOX_BUILDER_MEMORY`.

## Config and credentials

Each instance gets an isolated copy of selected harness settings; changes never sync back.
Pi also copies auth and installs portable npm/git package declarations from host settings.
Host-specific settings, local packages and Claude hooks/plugins are omitted. Use `--no-config`
to skip importing settings, packages and credentials, then log in inside the guest.

Claude's host login is **not copied**. Use `/login` in the guest, or create a separate token
with `claude setup-token` and supply `CLAUDE_CODE_OAUTH_TOKEN` to Devbox only, for example via
`op run -- devbox new claude`. Do not export it globally: host Claude would use it too.
The token reaches only the harness process, not the image, container configuration or saved state.

Only Git author identity and `COLORTERM` are inherited automatically. Other environment values
must be declared by the project or harness. Custom harness profiles live in
`$XDG_CONFIG_HOME/devbox/harnesses/<name>/`; use the [built-in profiles](src/devbox/assets/harnesses)
as examples for `profile.toml`, `Containerfile` and optional host-side `hooks.py`.

## Saved state and safety

Homes, copied config and conversations live in `$XDG_DATA_HOME/devbox/<backend>/instances/<id>/`
(default base `~/.local/share/devbox`; override the base with `--state-dir`). The backend is
`apple` or `podman`; their state is never mixed. Removing a container keeps this state.
`resume` snapshots and forks a conversation so the original never has two writers.

Legacy `<base>/instances` directories are left untouched and are not listed or cleaned.
To migrate, remove their containers first (preserving saved state), then move `instances`
into `<base>/apple/` or `<base>/podman/` according to the engine that created it. Do not
merge or overwrite an existing destination; split legacy state by backend if necessary.

`cleanup` previews orphaned saved instances and unused, ownership-labelled Devbox images,
then asks for confirmation. **Deleting saved instances permanently deletes their conversations
and copied credentials.** Use `cleanup --select` to choose instances with fzf. Running and
stopped containers protect state; retained instances also protect their images. Cleanup does
not remove containers, volumes, unrelated images or shared build caches.

On macOS each container is a VM; Linux rootless containers share the host kernel. Isolation
protects unmounted host files, **not the repository**: the harness can rewrite Git history,
modify scripts you later run, use provided credentials and access the network. There is no
host shell/socket/SSH-agent bridge. Trust manifests, Containerfiles and harness profiles as
executable configuration.

## Development

Use Python 3.15 for development; maintain Python 3.12 compatibility.
See [AGENTS.md](AGENTS.md) for the worktree and landing workflow.

```sh
uv sync --locked
uv run prek install
uv run pytest                        # no running container engine needed
uv run prek run --all-files           # lint, format, types and tests
```
