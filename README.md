# devbox

Run coding harnesses inside lightweight Linux VMs using [Apple's `container`](https://github.com/apple/container)
on macOS, or rootless [Podman](https://podman.io) containers on Linux.
The project is mounted writable at its original path; the rest of your home stays on the host.
Pi and Claude Code are the included harnesses.

## Install

Requires [uv](https://docs.astral.sh/uv/), and [fzf](https://github.com/junegunn/fzf) for
interactive pickers, plus a container engine:

- **macOS** (Apple silicon): Apple's `container` **1.5+**. When its service isn't running,
  Devbox offers to run `container system start` if both stdin and stdout are terminals.
  The prompt defaults to **No**; confirming starts the service and rechecks it before continuing.
  Startup failures stop the command. For non-interactive use, start the service yourself first.
- **Linux** (x86_64 or arm64): rootless Podman **4.3+** with cgroup v2. On Arch,
  `sudo pacman -S podman fzf uv`. Your user needs `/etc/subuid` and `/etc/subgid` entries
  (`sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER`); there is no
  service to start. `podman info` should report `rootless: true`.
  If containers or builds fail with `sd-bus call: Process org.freedesktop.systemd1 exited`,
  Podman can't reach systemd's user bus: make sure `DBUS_SESSION_BUS_ADDRESS` is
  `unix:path=$XDG_RUNTIME_DIR/bus` (a desktop started with `dbus-run-session` gets a private
  bus instead). Switching to cgroupfs in `~/.config/containers/containers.conf` also works, but
  rootless cgroupfs can't enforce `cpus`/`memory` limits, and Devbox warns about that:

  ```toml
  [engine]
  cgroup_manager = "cgroupfs"
  ```

```sh
cd ~/code/devbox
uv sync --locked
uv run prek install
uv tool install --editable .            # put devbox on PATH
```

Devbox picks the engine from the host OS; set `DEVBOX_ENGINE=apple` or `DEVBOX_ENGINE=podman`
to override it.

Optional shell completion (commands, options and paths):

```sh
eval "$(devbox completion bash)"      # ~/.bashrc
eval "$(devbox completion zsh)"       # ~/.zshrc, after compinit
devbox completion fish | source       # ~/.config/fish/config.fish
```

## Use

```sh
devbox new pi                         # --project defaults to .; omit the harness for a picker
devbox new claude
devbox new pi --detach
devbox ls                             # managed containers, including stopped ones
devbox attach [id]                    # IDs accept unique prefixes; omit for a picker
devbox shell [id]                     # plain non-root shell for inspection
devbox stop [id]                      # stops processes; container disk is kept
devbox rm [id]                        # removes the container and its artifact volumes
devbox kill-all [--yes]
devbox cleanup                          # confirm deletion of saved instances without containers
devbox cleanup --select                 # choose a subset with fzf multi-select, then confirm
devbox sessions                       # saved conversations, including from removed containers
devbox resume [session-id]            # fork a saved conversation into a new container
devbox build pi                       # rebuild the environment image
```

Inside the container, **Ctrl-a, then d** detaches and returns to the host shell; the harness keeps
running. Stopping a container ends its processes, so use `resume` to continue a conversation later.

## Project manifest

Put `.devbox.toml` in the project (see this repo's [`.devbox.toml`](.devbox.toml)):

```toml
version = 1

[image]
containerfile = ".devbox/Containerfile"
context = ".devbox"                  # keep source and .git out of the build context

[container]
cpus = 4
memory = "8G"

[workspace]
artifacts = [".godot", "tests/bin", "tests/obj", ".worktrees"]

[environment]
DOTNET_CLI_TELEMETRY_OPTOUT = "1"
```

The manifest is discovered upwards from `--project`. A Git worktree inside the checkout falls back
to the primary checkout's manifest; the whole primary checkout is mounted.

The project's Containerfile installs project dependencies only, on a Debian/Ubuntu (apt, glibc)
base. Devbox layers a terminal runtime and the selected harness on top.

### Artifacts and worktrees

Each `artifacts` path in the primary checkout is covered by a container-local volume, so Linux build
outputs never mix with the host's. Host contents are hidden, not copied or changed; empty mount-point
directories may appear on the host. Tracked files under an artifact path (such as a
`.worktrees/.gdignore`) are checked out into the volume. Volumes survive `stop` and are deleted by `rm`.

Listing `.worktrees` makes worktrees created inside the container guest-local, so their build outputs
are too. Host worktrees there are hidden from the guest, so launch from the primary checkout.
**Commit before removing a container**: Git objects and refs live in the shared `.git`, but
uncommitted worktree files are deleted with the container. Afterwards, `git worktree prune` cleans
up stale registrations.

### Image preparation

The first `new` builds the project, runtime and harness layers into a prepared image. Later launches
reuse it without starting the builder. Run `devbox build <harness>` after changing a Containerfile,
build context or Devbox itself; it also refreshes unpinned harness packages. If Devbox starts
the builder, it stops it again afterwards. A builder that was already running is left running.
On macOS the builder gets `DEVBOX_BUILDER_MEMORY` (default `4G`).

Pi and Claude Code install the same version as `pi --version`/`claude --version` on the host, so
upgrading on the host prepares a matching image at the next launch. Without a host installation,
the version pinned in the harness Containerfile is used.

## Harness config and credentials

Each container gets its **own copy** of selected harness config files. For Pi, these are settings,
keybindings, models, auth and user instructions from `~/.pi/agent`. Changes don't sync back.
Host-specific settings and local packages are dropped with a notice. Portable npm/git package
declarations are installed into the image at build time, so they must not contain credentials.
`--no-config` skips all of this; use `/login` inside Pi instead.

For Claude Code, these are `settings.json`, `keybindings.json` and `CLAUDE.md` from `~/.claude`.
Hooks, status line, credential helpers, plugins and extra directories are dropped with a notice,
and first-run onboarding is skipped. The in-container auto-updater is disabled.

The host's Claude login (in the macOS Keychain, or `~/.claude/.credentials.json` on Linux) is never
copied: a shared login would rotate tokens out from under the host. Instead, create a separate
long-lived token once with `claude setup-token` (valid for a year) and set
`CLAUDE_CODE_OAUTH_TOKEN` for devbox only. Don't export it to your whole shell: the host's
`claude` would use it instead of its own login. For example, wrap the launch with a
password-manager CLI (`op run -- devbox new claude`), or keep the token in a file only you can
read and use a shell function:

```fish
function devbox --wraps devbox
    CLAUDE_CODE_OAUTH_TOKEN=(cat ~/.config/devbox/claude-token) command devbox $argv
end
```

New Claude containers then start logged in. The token is passed only to the harness process's
environment (not the container configuration, image or saved state). Without one, use `/login`
inside the container. `--no-config` skips forwarding.

Only Git `user.name`/`user.email` (as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`) and `COLORTERM` are passed
in; the manifest and profile can override `COLORTERM`. No other host environment is forwarded beyond
what they declare.

## Saved state

State lives under `$XDG_DATA_HOME/devbox` (override with `--state-dir`):

```text
instances/<id>/
  metadata.json
  home/                                 # the guest's /home/agent: copied config and sessions
```

Removing containers never removes this state. `resume` snapshots a session into a new instance and
the harness forks it, so the original never has two writers.

`devbox cleanup` lists saved instances whose containers no longer exist and asks before permanently
deleting their homes, **including conversations and copied credentials**. Use `--select` to choose
only some instances with `fzf` multi-select (Tab to mark, Enter to review). Declining or cancelling
deletes nothing. Instances with running **or stopped** containers are kept; the container service
must be available to check, and containers are checked again after confirmation. The command reports
the number removed and approximate disk space recovered, based on allocated blocks without following
symlinks. Filesystem compression, shared/cloned blocks and snapshots can affect actual free space.
Cleanup only deletes saved instance directories; it does not remove containers, images or volumes.

The `resume` picker and `sessions` list show aligned columns for the harness, project, title,
then the full session ID (explicit IDs and prefixes still work as before):

```text
pi      project  Fix login flow  devbox-aabbccdd/session-id
claude  project  Check tests     devbox-eeff0011/session-id
```

Projects display just their final directory name when it identifies one distinct saved project
path. Multiple sessions for the same project still use the short name. If different projects
share that directory name, both show their full paths. JSON output always retains the full path.

Titles come from saved harness metadata: Pi session names (`/name`, including names set by
extensions), or Claude custom titles (`/rename`), automatic titles, and session summaries, in
that preference order. The latest title of each kind wins. Sessions without a saved title show
`(untitled)`; prompt and tool-output text is never used as a fallback. Titles are single-line,
terminal-safe, and limited to 100 characters. `sessions --json` includes a nullable `title` field.

## Additional harnesses

User profiles override or add to the built-ins in `$XDG_CONFIG_HOME/devbox/harnesses/<name>/`:

- `profile.toml`: the command, optional `resume_args` with `{session}` (snapshot path) or
  `{session_id}`, `state_dir`/`session_dir` under `/home/agent`, optional `session_glob` (default
  `**/*.jsonl`), `seed_dir` and `seed_files` to copy, `environment`, optional `prepared_dirs`,
  an optional host `version_command` whose version is passed as the `HARNESS_VERSION` build arg,
  and optional `secrets`: environment variable names forwarded from the host environment to the
  harness process when set.
  Follow [the Pi profile](src/devbox/assets/harnesses/pi/profile.toml).
- `Containerfile`: takes `ARG RUNTIME_IMAGE` and builds `FROM` it.
- Optional `hooks.py`: host-side `build_files(seed_dir)` returns extra build-context files (keyed
  into the image name), `adapt_seed(state_dir)` adjusts copied config, and
  `resume_file(session_id, cwd)` places the resume snapshot (default `.devbox/resume.jsonl`).
  `session_title(records)` optionally returns a saved display title (or `None`); `records` is a
  streaming iterator of complete JSON objects from the transcript. Malformed, incomplete, and
  oversized records (over 64 KiB) are skipped. Extract title metadata only, not prompt text.
  Invalid profiles and failing title hooks only drop titles; listing and resuming still work.

Each `prepared_dirs` entry is symlinked from the home to `/opt/devbox/prepared/<path>` in the
image. Every container gets its own writable copy.

## Security scope

On macOS each container is its own VM. On Linux, rootless Podman containers share the host kernel
but run in your user namespace, with your host user mapped to the guest `agent` user, so files the
agent creates in the checkout are owned by you.

The isolation protects unmounted host files and processes, **not the repository**. The agent can rewrite
history, change hooks and scripts you later run on the host, use copied or stored credentials, and
reach the network. There is no host shell bridge, socket or SSH agent. Treat manifests, Containerfiles and
harness profiles as trusted executable configuration.

## Development

```sh
uv run ruff check . --fix && uv run ruff format .
uv run ty check .
uv run pytest                          # needs neither container engine
uv run prek run --all-files
```

To work on Devbox inside Devbox, run `devbox new pi --project ~/code/devbox`, then
`uv sync --locked` in the guest. Create worktrees inside the guest.
