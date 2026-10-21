# agentbox

Run coding harnesses inside lightweight Linux ARM64 VMs using [Apple's `container`](https://github.com/apple/container).
The project stays writable at its original path; the rest of your home stays outside the VM.
Pi is the first included harness. The name is provisional.

## Install

Requirements: Apple silicon macOS with `container` **1.5+**, [uv](https://docs.astral.sh/uv/), and
[fzf](https://github.com/junegunn/fzf) for interactive pickers. Project images currently need a
Debian/Ubuntu base with apt, Python 3 support and standard Linux utilities. No Docker daemon is used.

```sh
cd ~/code/agentbox
uv sync --locked                       # development interpreter: Python 3.15
uv run prek install                    # local hooks invoke this venv's tools through uv
uv tool install --editable .            # put agentbox on PATH
container system start                 # first start may offer the recommended kernel download
```

The Python package supports 3.12+. `uv run` in this checkout uses `.python-version` (3.15).

## Use

```sh
agentbox new pi                         # --project defaults to .
agentbox new                            # fzf harness picker
agentbox new pi --project ~/my-project
agentbox new pi --detach                 # useful for scripting
agentbox ls                             # managed containers only, including stopped ones
agentbox attach                         # fzf running-container picker
agentbox attach <id-or-short-prefix>
agentbox shell [id]                      # independent, non-root inspection shell
agentbox stop [id]                       # stops processes, retains container disk + history
agentbox rm [id]                         # stops/removes container; retains saved history
agentbox kill-all                        # confirm stop/removal of our containers only
agentbox kill-all --yes                  # explicit noninteractive cleanup
agentbox sessions                       # saved conversations, even from removed containers
agentbox resume [session-id]             # snapshot + fork into a NEW container
agentbox build pi                       # explicitly prepare/rebuild the environment
agentbox build pi --keep-builder        # opt out of owned-builder cleanup
```

Omitting a harness, container ID or session ID opens fzf. Explicit arguments bypass pickers.
Selection requires a terminal; there is no hidden prompt in noninteractive invocations. `new` and
`resume` require a terminal unless `--detach` is supplied. `ls` and `sessions` also support `--json`.

Inside the container, **Ctrl-a, then d** detaches the inner tmux and returns directly to the host
shell. Pi continues running, even if it is generating an answer. Outer tmux can keep Ctrl-b.
Each instance gets a stable accent color and a labeled status line. Host tmux config is not mounted.
`shell` is a plain inspection shell, not a preserved tmux session; use `exit` or Ctrl-d to leave it.

The container's init/keepalive is independent of tmux. If Pi exits, the VM remains available for
inspection. Detachment preserves live processes; **stopping a VM does not**. Use `resume` to start a
new process with a saved conversation. The wrapper does not attempt to checkpoint processes.

## Project-owned setup

Put `.agentbox.toml` in the project. Configuration is discovered upwards; Git worktrees can fall back
to the primary checkout's configuration. The full primary checkout is mounted, not just the selected
worktree. Worktrees must be inside it, usually under `.worktrees/`.

```toml
version = 1

[image]
containerfile = ".agentbox/Containerfile"
context = ".agentbox"                  # small context: don't send assets/.git to BuildKit

[container]
cpus = 4
memory = "8G"                        # guest RAM capacity, not an idle-footprint promise

[builder]
memory = "4G"                        # independent image-builder capacity (default)

[workspace]
artifacts = [".godot", "tests/bin", "tests/obj"]
prepare_commands = ["git", "godot", "dotnet"]

[environment]
DOTNET_CLI_TELEMETRY_OPTOUT = "1"
```

The project's Containerfile installs project dependencies only. Agentbox composes a cached common
terminal/runtime layer and then the selected harness layer. Images are standard OCI Linux images;
only the host lifecycle adapter depends on Apple's CLI.

### Preparation versus launch

The first `new` prepares a missing image. Subsequent `new`/`resume` calls reuse it **without any
BuildKit startup, build, or OCI/tar export**. The image is scoped to the project, state store, harness,
and selected personal package sources; changing only models, credentials or package resource filters
does not rebuild it. `--no-config` uses a package-free variant. Removing an image makes the next
launch prepare it again.

Use `agentbox build pi` after changing a Containerfile, build context, runtime/harness implementation,
or other image inputs. Ordinary source edits do not require it. Explicit builds refresh unpinned
personal packages; package versions remain at that snapshot until rebuilt. There is deliberately no
background freshness check, full-repository hash scan, or automatic base-image update on launch.
Existing pre-preparation Agentbox images are rebuilt once into the new prepared-image namespace.

The builder is leased under a per-user lock shared across all Agentbox projects and `--state-dir`
values. A builder already running is left alone, including its resource settings. If Agentbox starts
it, it attempts to **stop** it after success, failure or Ctrl-C, preserving its disk cache and images.
`--keep-builder` on `build`, `new` or `resume` retains it intentionally. SIGKILL/host crashes cannot run
cleanup; use `container builder stop` once no build is active. Apple's shared builder has no exclusive
ownership lease: raw `container build` jobs do not participate in our lock and must not overlap an
Agentbox-owned temporary builder. Start it yourself beforehand or use `--keep-builder` when sharing.

Builder capacity is separate from guest capacity. Neither is an idle-memory target: Linux file cache
can retain substantial memory inside a VM after a build. Guest-available memory and host RSS alone
are not substitutes for macOS physical-footprint accounting. No builder is needed to keep agents alive.
CLI timings distinguish environment preparation from VM/runtime readiness; the latter is **not** a
claim that Pi has finished rendering its UI.

### Generated-directory isolation

A plain bind mount cannot exclude children. The guest preparation helper bind-mounts VM-local
cache directories over the declared artifact paths, without copying host contents. It applies to
all in-repo worktrees, including worktrees created after launch. The helper runs before the declared
commands and after `git worktree add` (even if a hook fails after checkout). Empty mount-point directories may be created on
the host; generated contents stay in the VM. This is directory masking, not a general filesystem
filter: commands that bypass the shims by absolute path must not build a newly created worktree
until preparation has run.

The agent is a non-root guest user. Only the root-owned preparation helper is sudo-enabled, without
arguments, and its configuration is mounted read-only. The guest gets `CAP_SYS_ADMIN` for those
mounts. This is not a claim of a hardened guest-user/root boundary; the security boundary is the VM
and what you share with it. Artifact paths may not traverse symlinks or overlap one another.

Caches survive reconnects and remain on a stopped VM's disk; removing the VM deletes them. Each fresh
container/worktree must import Godot assets once. Remove containers before removing/renaming their
mounted worktrees. Use the host's editor for graphical playtesting.

## Configuration and credentials

Each instance has its **own writable harness state**, not a shared writable harness home. Pi's profile
copies selected files from `~/.pi/agent`: preferences, keybindings, model definitions, saved auth and
user instructions. It does not import host sessions, trust decisions, MCP servers, installed package
trees, local extensions, or your whole home. Portable personal npm/git package declarations are
prepared as Linux dependencies in the harness image, with no copied settings or credentials in the
build context. Package declarations/layers are image-visible: use credential-free sources, not URLs
containing tokens. Private registry/SSH authentication is not forwarded to builds. Host-specific
settings are omitted with a notice.

At startup, prepared npm/git directories are cloned to private VM-local writable storage and cover
only those directories in the instance home. This avoids copying package trees through the host
filesystem on every launch. Settings, auth and sessions remain private and host-backed. Package edits
cannot affect another VM or the image seed; stop retains them, removal discards them. Project-scoped
packages still follow Pi's trust flow and are not preinstalled merely by reading an untrusted project.
Pi's `fd` executable is provisioned in the image rather than downloaded on first launch.

Use `--no-config` to skip imports and `/login` inside Pi instead. Copied auth and configuration do not
sync back to the host. OAuth refresh/revocation behavior varies by provider: you may need to log in
again, and copying a refresh credential does not guarantee independently renewable logins. Custom
themes or model definitions that invoke host programs may need container-specific configuration.

Only the effective Git `user.name` and `user.email` are copied into guest Git configuration—not
host credential helpers, hooks settings, signing commands or URL rewrites. The runtime trusts the
specific mounted checkout/worktree paths to accommodate VirtioFS ownership. Projects using Git LFS
must install/configure it in their base image so shared working files are recognized correctly and
checkout hooks work.

No host environment is broadly forwarded; profiles and project manifests explicitly declare guest
environment variables. Do not put secrets in project manifests. Credentials copied into a VM are
readable by its agent and should be scoped appropriately.

## Durable sessions

By default, host state lives under `$XDG_DATA_HOME/agentbox` (usually `~/.local/share/agentbox`):

```text
instances/<id>/
  metadata.json
  control/                              # read-only configuration mount in the guest
  home/                                 # private writable guest home and durable harness state
    .pi/agent/sessions/
```

Use global `--state-dir PATH` to override this location. The parent directory must be trusted/private;
new files/directories are created with a private umask. Session transcripts can contain source and
secrets. A container has access only to its own state, not all saved conversations.

Removing containers **never** removes this state. `resume` snapshots complete JSONL records into a
new instance and Pi forks them, so the original session never has two writers. Running processes,
temporary files and guest-only build products are not restored. Pi receives a short environment note
explaining the new Linux execution environment. Session lists display IDs/project metadata, not
conversation contents. A host Pi can also resume a saved file using `pi --session <path>`; that is
outside agentbox's ownership and you must avoid concurrent writers yourself.

There is no automatic history purge. Delete an instance's saved directory explicitly when you no
longer want its conversations **and copied credentials**, after removing its container.

## Additional harnesses

User profiles override/add to built-ins under:

```text
$XDG_CONFIG_HOME/agentbox/harnesses/<name>/
  profile.toml
  Containerfile
```

A profile follows `src/agentbox/assets/harnesses/pi/profile.toml`: command argv, optional resume argv
with `{session}`, relative state/session directories beneath `/home/agent`, selected seed files and
environment variables. Its Containerfile takes global `ARG RUNTIME_IMAGE` and derives from that image.
The runtime image already provides the agent user, tmux and the entrypoint; preserve them. Install
Linux dependencies rather than mounting host binaries or `node_modules`.

Optional `prepared_dirs` lists relative home directories to seed from matching paths beneath
`/opt/agentbox/prepared` in the image. These directories get private writable guest-local copies;
they may not overlap each other or the durable `session_dir`. Images for custom profiles must provide
those templates. The generated `package-sources.json` in a harness build context contains only selected
portable personal Pi package sources (empty for other harnesses); the included Pi build helper consumes
it. Additional harness package discovery needs its own adapter, not Pi settings assumptions.

The current session catalog handles local **JSONL** conversations; remote-only harnesses can omit
resume support. Harnesses with another storage format will need a small catalog adapter rather than
pretending all transcripts are interchangeable. Codex and Claude profiles are intentionally not yet
included.

## Security scope

This protects unmounted host files and processes, **not the repository**. The agent can edit main,
rewrite Git history, delete repository files, use copied credentials and contact reachable host/LAN
services. Repository edits can also change hooks/scripts that execute later when you run Git, builds
or the editor on the host. Review changes before executing them there. Follow project worktree policies
and maintain backups. There is no host shell bridge,
container-management socket, SSH agent, or blanket host-home mount. Egress is not filtered; this is
not an API-spending limiter or an exfiltration-proof sandbox. Treat project manifests/Containerfiles
and harness profiles as trusted executable configuration.

## Development and verification

```sh
uv run ruff check . --fix
uv run ruff format .
uv run ty check .
uv run pytest
uv run prek run --all-files
```

Unit tests do not start containers. Before shipping runtime changes, also build a real project and
verify: non-root operation; host generated-file sentinels remain untouched in existing and new
worktrees; headless import/build/tests; Ctrl-a d detach/reconnect in an actual terminal; durable
history and forked resume after removal; cleanup leaves unrelated containers alone. Host-side session
reads reject symlink components in agent-writable state, including when snapshotting a live session. See
`docs/verification.md` for checks performed and outstanding manual checks.
