# agentbox

Run coding harnesses inside lightweight Linux VMs using [Apple's `container`](https://github.com/apple/container).
The project is mounted writable at its original path; the rest of your home stays on the host.
Pi and Claude Code are the included harnesses. The name is provisional.

## Install

Requires Apple silicon macOS with `container` **1.5+**, [uv](https://docs.astral.sh/uv/), and
[fzf](https://github.com/junegunn/fzf) for interactive pickers.

```sh
cd ~/code/agentbox
uv sync --locked
uv run prek install
uv tool install --editable .            # put agentbox on PATH
container system start
```

Optional shell completion (commands, options and paths):

```sh
eval "$(agentbox completion bash)"      # ~/.bashrc
eval "$(agentbox completion zsh)"       # ~/.zshrc, after compinit
agentbox completion fish | source       # ~/.config/fish/config.fish
```

## Use

```sh
agentbox new pi                         # --project defaults to .; omit the harness for a picker
agentbox new claude
agentbox new pi --detach
agentbox ls                             # managed containers, including stopped ones
agentbox attach [id]                    # IDs accept unique prefixes; omit for a picker
agentbox shell [id]                     # plain non-root shell for inspection
agentbox stop [id]                      # stops processes; container disk is kept
agentbox rm [id]                        # removes the container and its artifact volumes
agentbox kill-all [--yes]
agentbox sessions                       # saved conversations, including from removed containers
agentbox resume [session-id]            # fork a saved conversation into a new container
agentbox build pi                       # rebuild the environment image
```

Inside the container, **Ctrl-a, then d** detaches and returns to the host shell; the harness keeps
running. Stopping a container ends its processes, so use `resume` to continue a conversation later.

## Project manifest

Put `.agentbox.toml` in the project (see this repo's [`.agentbox.toml`](.agentbox.toml)):

```toml
version = 1

[image]
containerfile = ".agentbox/Containerfile"
context = ".agentbox"                  # keep source and .git out of the build context

[container]
cpus = 4
memory = "8G"

[builder]
memory = "4G"

[workspace]
artifacts = [".godot", "tests/bin", "tests/obj", ".worktrees"]

[environment]
DOTNET_CLI_TELEMETRY_OPTOUT = "1"
```

The manifest is discovered upwards from `--project`. A Git worktree inside the checkout falls back
to the primary checkout's manifest; the whole primary checkout is mounted.

The project's Containerfile installs project dependencies only, on a Debian/Ubuntu (apt, glibc)
base. Agentbox layers a terminal runtime and the selected harness on top.

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
reuse it without starting the builder. Run `agentbox build <harness>` after changing a Containerfile,
build context or Agentbox itself; it also refreshes unpinned harness packages. If Agentbox starts
the builder, it stops it again afterwards. A builder that was already running is left running.

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
`CLAUDE_CODE_OAUTH_TOKEN` for agentbox only. Don't export it to your whole shell: the host's
`claude` would use it instead of its own login. For example, wrap the launch with a
password-manager CLI (`op run -- agentbox new claude`), or keep the token in a file only you can
read and use a shell function:

```fish
function agentbox --wraps agentbox
    CLAUDE_CODE_OAUTH_TOKEN=(cat ~/.config/agentbox/claude-token) command agentbox $argv
end
```

New Claude containers then start logged in. The token is passed only to the harness process's
environment (not the container configuration, image or saved state). Without one, use `/login`
inside the container. `--no-config` skips forwarding.

Only Git `user.name`/`user.email` (as `GIT_AUTHOR_*`/`GIT_COMMITTER_*`) and `COLORTERM` are passed
in; the manifest and profile can override `COLORTERM`. No other host environment is forwarded beyond
what they declare.

## Saved state

State lives under `$XDG_DATA_HOME/agentbox` (override with `--state-dir`):

```text
instances/<id>/
  metadata.json
  home/                                 # the guest's /home/agent: copied config and sessions
```

Removing containers never removes this state. `resume` snapshots a session into a new instance and
the harness forks it, so the original never has two writers. Delete an instance directory yourself
to discard its conversations **and copied credentials**.

## Additional harnesses

User profiles override or add to the built-ins in `$XDG_CONFIG_HOME/agentbox/harnesses/<name>/`:

- `profile.toml`: the command, optional `resume_args` with `{session}` (snapshot path) or
  `{session_id}`, `state_dir`/`session_dir` under `/home/agent`, optional `session_glob` (default
  `**/*.jsonl`), `seed_dir` and `seed_files` to copy, `environment`, optional `prepared_dirs`,
  an optional host `version_command` whose version is passed as the `HARNESS_VERSION` build arg,
  and optional `secrets`: environment variable names forwarded from the host environment to the
  harness process when set.
  Follow [the Pi profile](src/agentbox/assets/harnesses/pi/profile.toml).
- `Containerfile`: takes `ARG RUNTIME_IMAGE` and builds `FROM` it.
- Optional `hooks.py`: host-side `build_files(seed_dir)` returns extra build-context files (keyed
  into the image name), `adapt_seed(state_dir)` adjusts copied config, and
  `resume_file(session_id, cwd)` places the resume snapshot (default `.agentbox/resume.jsonl`).

Each `prepared_dirs` entry is symlinked from the home to `/opt/agentbox/prepared/<path>` in the
image. Every container gets its own writable copy.

## Security scope

The VM protects unmounted host files and processes, **not the repository**. The agent can rewrite
history, change hooks and scripts you later run on the host, use copied or stored credentials, and
reach the network. There is no host shell bridge, socket or SSH agent. Treat manifests, Containerfiles and
harness profiles as trusted executable configuration.

## Development

```sh
uv run ruff check . --fix && uv run ruff format .
uv run ty check .
uv run pytest                          # no container service needed
uv run prek run --all-files
```

To work on Agentbox inside Agentbox, run `agentbox new pi --project ~/code/agentbox`, then
`uv sync --locked` in the guest. Create worktrees inside the guest.
