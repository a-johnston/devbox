# devbox

Run Pi or Claude Code in disposable Linux environments: a VM per container with
[Apple's `container`](https://github.com/apple/container) on macOS, or rootless
[Podman](https://podman.io) containers on Linux. Each instance works in its own clone of
your checkout, mounted at the original path; your working tree, `.git` and the rest of your
home stay on the host. You fetch its branches for review. Conversations survive container removal.

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
devbox init                           # template .devbox.toml and .devbox/Containerfile
devbox new pi                         # project defaults to .; omit harness for a picker
devbox new claude --detach
devbox ls                             # running and stopped managed containers
devbox attach [id]                    # omit ID for a picker; unique prefixes work
devbox shell [id]                     # non-root inspection shell
devbox stop [id ...]                  # stop processes, keep container disk
devbox rm [id ...]                    # remove container, keep history and clone
devbox fetch [id]                     # fetch new work as local <short-instance>/<branch>
devbox review [id] [branch]           # fetch, pick a branch and show its diff
devbox land [id] [branch]             # fetch, rebase onto the current host branch, ff-merge
devbox sessions                       # saved conversations, including removed containers
devbox sync [id] [branch ...]         # deliver host branches to an instance as host/<branch>
devbox resume [session-id]            # fork a conversation into a new container
devbox build pi                       # rebuild the project/runtime/harness image
devbox cleanup                       # confirm deletion of orphaned state and unused images
devbox cleanup --only images         # or --only instances
```

New instances take a random free color name, such as `devbox-red`, and their tmux status line
uses that color. Saved state keeps a name taken until `cleanup`; once every color is taken,
instances get a random ID instead.
`stop` and `rm` accept multiple IDs or unique prefixes (`red`); omit IDs for a multi-select picker.

`attach` and `shell` pass your `TERM` through when the guest has its terminfo, otherwise
`xterm-256color`; install a custom entry in the project image to keep it.
All harnesses share a terminal runtime with Vim installed and `EDITOR=vim`, `VISUAL=vim`,
for shell commands and harness actions that open messages in an external editor. These
are guest defaults, not copied from the host; override them in the manifest's `[environment]`
if needed (the selected editor must be installed in the guest).
**Ctrl-a, then d** detaches without stopping the harness. After `stop`, use `resume` to
continue a conversation. `kill-all` removes all managed containers but keeps saved history.
See `devbox --help` and `<command> --help` for options, including JSON output and shell completion.

## Project setup

`devbox init` writes a template `.devbox.toml`, listing every option, and an Ubuntu
`.devbox/Containerfile` at the checkout's top level, keeping any that already exist.
A manifest looks like this (see also [this repo's](.devbox.toml)):

```toml
version = 1

[image]
containerfile = ".devbox/Containerfile"
context = ".devbox"                  # keep source and .git out of the build context

[container]
cpus = 4
memory = "8G"

[environment]
MY_SETTING = "value"

[review]
watch = ["prek.toml"]                 # flagged by fetch, in addition to the image inputs
```

The project Containerfile installs dependencies on a Debian/Ubuntu (apt, glibc) base;
Devbox adds the terminal runtime and harness. Manifests are discovered upwards from
`--project`, with a fallback to the primary checkout for in-repo Git worktrees. The image
context and Containerfile must be inside the project and outside `.git`.

Each instance clones the committed `HEAD` of the checkout you launch from (a worktree or
the primary) into its saved state, and mounts the clone at the checkout's path. Uncommitted
changes are not copied. The clone has no remote: the agent commits to branches, and
`devbox fetch` copies them into `refs/devbox/<id>/` in your repository. For branches with
commits beyond the instance's starting point, it publishes local branches named
`<short-instance>/<branch>` (for example, `orange/harness-vim` from `devbox-orange`), so
Git can list and tab-complete them without mixing different instances' work. Branches with
`0 new commits` are still tracked and reported, but do not create or update local branches.
It reports new commits, branches rewritten since the last fetch, and changes to
the manifest, Containerfile, build context and `[review] watch` paths (Git glob pathspecs).
Watched paths always come from your manifest, not the clone's. `devbox review [id] [branch]`
fetches, picks a branch (with fzf when more than one has new commits) and shows its changes
since the instance started with `git diff` in your repository; `--stat` summarizes them.
After review, `devbox land [id] [branch]` fetches again, selects a branch the same way as
`review`, rebases its local `<short-instance>/<branch>` branch onto the branch currently
checked out in the host repository's **primary checkout**, then merges with `--ff-only`.
It returns that checkout to the target branch on success. The target is not necessarily
`main` or the branch the instance started from; check out the intended target there first.
Instance IDs and unique prefixes work even after container removal, and omitting the ID
opens the usual picker. Landing requires a clean host checkout (including untracked files),
no Git operation in progress, and an unchanged fetched local branch not checked out in any
worktree. It never stashes changes or runs Git in the instance clone.

A rebase conflict fails the command and leaves the rebase in progress in the primary
checkout; nothing is merged and the instance clone stays untouched. Resolve it there with
`git rebase --continue`, then check out the target branch and `git merge --ff-only` the
rebased local branch. Alternatively, run `git rebase --abort`, check out the target branch,
and use `devbox sync <id> <target-branch>` to let the agent fix the conflict in the instance
before reviewing and landing again. Landing does not run project checks or delete branches,
instances or conversations. You can also land fetched branches manually using Git.

Later fetches refresh a local branch only if
it still points to the last fetched commit and is not checked out in any worktree. Existing
branches with different tips, checked-out branches and naming conflicts are left alone with
a warning; the `refs/devbox/<id>/` refs still update and remain the source for review. Local
branches are not pruned when an instance branch disappears or loses its new commits;
legacy `devbox/<id>/<branch>` branches are also left untouched. Remove them with Git when no
longer needed. Do not run Git or project tooling inside a clone on the host: its config and
hooks are agent-controlled.

`devbox sync [id] [branch ...]` delivers host branches to a running instance as
`host/<branch>` refs (default: the branch it started from), so the agent can rebase onto
newer work. Only the named branches leave your repository, as a Git bundle piped into the
guest. `resume` clones the previous instance's repository when it still exists; pass
`--from host` to start from your checkout instead.

Agents learn these constraints from Devbox's [instructions](src/devbox/assets/runtime/environment.md),
which every harness appends to its system prompt, and `DEVBOX_INSTANCE` holds the instance
ID. Project agent files need not repeat them.

The first launch builds an image; subsequent launches reuse it. Run `devbox build <harness>`
after changing Containerfiles, build contexts or Devbox. Harness versions follow the host
installation when available, otherwise a pinned default. On macOS, builder memory defaults
to `4G`; override with `DEVBOX_BUILDER_MEMORY`.

## Config and credentials

Each instance gets an isolated copy of selected harness settings; changes never sync back.
Pi installs portable npm/git package declarations from host settings. Host-specific settings,
local packages and Claude hooks/plugins are omitted. Use `--no-config` to skip importing
settings/packages/credentials and mounting shared harness state, then log in independently
inside the guest.

Pi's host `auth.json` is **never copied**. Instead, Pi instances share a separate Devbox login
in `$XDG_CONFIG_HOME/devbox/shared/pi/auth/` (default `~/.config/devbox/shared/pi/auth/`).
Only this private directory is mounted read/write: `auth.json` and its temporary lock share
one filesystem, so Pi coordinates token refreshes across instances. Log in with `/login` in
one guest; new and resumed instances reuse that login. `/logout` affects all participating
instances. Shared auth survives container removal and `cleanup`, and is independent of
`--state-dir`. `--no-config` uses instance-local auth instead.

Pi uses a small SDK-based interactive launcher, not a Pi fork. Settings, packages and
conversations remain per-instance; the rest of the host Pi directory is never mounted.
Extensions that directly access the default auth path do not participate in shared auth.
See [Pi's profile notes](src/devbox/assets/harnesses/pi/README.md) for launcher limitations,
optional host use, and upgrading existing installations. Rebuild with `devbox build pi`
after upgrading Devbox; old running instances retain their original credential copies.

Claude's host login is **not copied**. Use `/login` in the guest, or create a separate token
with `claude setup-token` and supply `CLAUDE_CODE_OAUTH_TOKEN` to Devbox only, for example via
`op run -- devbox new claude`. Do not export it globally: host Claude would use it too.
The token reaches only the harness process, not the image, container configuration or saved state.

Only Git author identity, `COLORTERM` and, for `attach` and `shell`, `TERM` are inherited
automatically. Other environment values must be declared by the project or harness. Custom
harness profiles live in `$XDG_CONFIG_HOME/devbox/harnesses/<name>/`; see
[adding a harness](src/devbox/assets/harnesses/README.md).

## Saved state and safety

Homes, repository clones, copied config and conversations live in `$XDG_DATA_HOME/devbox/<backend>/instances/<id>/`
(default base `~/.local/share/devbox`; override the base with `--state-dir`). The backend is
`apple` or `podman`; their state is never mixed. Removing a container keeps this state.
`resume` snapshots and forks a conversation so the original never has two writers.

Legacy `<base>/instances` directories are left untouched and are not listed or cleaned.
To migrate, remove their containers first (preserving saved state), then move `instances`
into `<base>/apple/` or `<base>/podman/` according to the engine that created it. Do not
merge or overwrite an existing destination; split legacy state by backend if necessary.

`cleanup` previews orphaned saved instances and unused, ownership-labelled Devbox images,
then asks for confirmation. **Deleting saved instances permanently deletes their conversations,
unfetched clones and copied credentials.** Use `cleanup --select` to choose instances and then images with fzf;
only images freed by the chosen instances (or used by no instance) are offered. Limit the
scope with `--only instances` or `--only images`; an image-only cleanup still keeps images
that retained instances use. Running and
stopped containers protect state; retained instances also protect their images. Cleanup does
not remove containers, unrelated images, shared harness authentication, or shared build caches.
The confirmation shows approximate totals for both, counting each image ID once; its
summary reports approximate space recovered separately for instance folders and images.
Image estimates count each deleted image once, only after its last reference is removed, and
use the engine's full image sizes; shared layers and build caches can make actual recovery lower.
When some image sizes are unavailable, totals cover only the known sizes (shown as `>N`);
an estimate with no known sizes is reported as unavailable rather than zero.

On macOS each container is a VM; Linux rootless containers share the host kernel and run
with `no-new-privileges` and no capabilities. Devbox refuses a krun default
runtime: krun cannot `exec` inside its microVM, so the harness would run outside it. Isolation
protects unmounted host files and your repository: repository edits stay in its private clone,
and fetched work reaches your branches only when you merge it. Profiles can explicitly mount
small shared state directories (Pi's auth, for example); guests can change that shared state.
It can still use provided
credentials and access the network, and code you merge runs when you run it. There is no
host shell/socket/SSH-agent bridge. Trust manifests, Containerfiles and harness profiles as
executable configuration, and review changes to them that `fetch` flags.

## Development

Use Python 3.15 for development; maintain Python 3.12 compatibility.
See [AGENTS.md](AGENTS.md) for the worktree and landing workflow, and
[adding a container engine](src/devbox/engine/README.md) for new backends.

```sh
uv sync --locked
uv run prek install
uv run pytest                        # no running container engine needed
uv run prek run --all-files           # lint, format, types and tests
```
