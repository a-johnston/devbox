# Harness profiles

A harness profile tells devbox how to install, configure and launch one coding agent. Built-in
profiles live here. To add your own, create `$XDG_CONFIG_HOME/devbox/harnesses/<name>/` (default
`~/.config/devbox/harnesses/<name>/`). A user profile with the same `name` replaces a built-in
one. Use [`pi`](pi) and [`claude`](claude) as examples.

```
<name>/
  profile.toml     required: launch command, state layout and host config to copy
  Containerfile    required: installs the harness on top of the runtime image
  hooks.py         optional: host-side Python adapting copied config
  ...              anything else the Containerfile COPYs (the directory is the build context)
```

Profiles are executable configuration: the Containerfile runs at build time and `hooks.py`
runs on the host. Install only profiles you trust.

## profile.toml

| Key | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Lowercase name used on the command line (`[a-z][a-z0-9-]*`). |
| `command` | yes | Launch command, run in tmux in the project directory. |
| `state_dir` | yes | The harness's config/state directory, relative to `/home/agent`. Seed files are copied here. |
| `session_dir` | yes | Where it saves conversations, relative to `/home/agent`. Kept after container removal. |
| `session_glob` | no | Conversation files under `session_dir` (default `**/*.jsonl`). The first line must be JSON; its `id`, or else the file name, is the session ID. |
| `resume_args` | no | Arguments appended to `command` to resume a forked copy of a saved session. Use `{session}` (the snapshot's guest path) or `{session_id}`. Omit if the harness cannot resume. |
| `seed_dir` | yes | Host config directory to copy from, such as `~/.pi/agent`. Never mounted. |
| `seed_files` | no | Files copied from `seed_dir` into `state_dir` per instance (symlinks are skipped). |
| `prepared_dirs` | no | Directories, relative to `/home/agent`, prepared at build time under `/opt/devbox/prepared/` and linked into each home, e.g. installed packages. |
| `[shared_dirs]` | no | Explicit read/write shared state: lowercase `name = "guest/relative/path"`. Each name mounts `$XDG_CONFIG_HOME/devbox/shared/<harness>/<name>/` and sets `DEVBOX_SHARED_<NAME>` to its guest path. Skipped with `--no-config`. |
| `version_command` | no | Host command printing the installed version; it becomes the `HARNESS_VERSION` build argument so the guest matches the host. |
| `secrets` | no | Variable names read from the host environment or `[secrets]` in `$XDG_CONFIG_HOME/devbox/config.toml` (default `~/.config/devbox/config.toml`). Nonempty environment values take precedence. Forwarded only to the harness process, never to images, container config or saved state; skipped with `--no-config`. |
| `[environment]` | no | Plain `NAME = "value"` variables for the container. Never put secrets here. |

Load devbox's instructions so the agent knows its constraints (a private clone, `devbox fetch`
and `devbox sync`, no host bridge): pass `/home/agent/.devbox/environment.md` to the harness's
option for appending to its system prompt in `command`, as both built-in profiles do. If the
harness has no such option, point its global instructions file at it instead.

## Shared state

Unlike seed files, shared directories have live writers from multiple instances. They must
not overlap the harness state/session/prepared directories, each other, or devbox's runtime
files. Keep their contents limited to intentionally shared state, not executable settings or
conversation history. Mount the directory containing both a credential and its ephemeral
lock; mounting only the file or an already-existing lock directory cannot coordinate refreshes.
The harness owns synchronization; Devbox does not read, copy or merge shared contents.
Directories are private (mode `0700`), not included in images, independent of `--state-dir`,
and never removed by instance cleanup. Names must match `[a-z][a-z0-9_]*`.

For example, Pi declares `[shared_dirs] auth = ".devbox/pi-auth"`. This exposes only the
Devbox-owned auth directory, never `~/.pi/agent`, and its launcher uses `DEVBOX_SHARED_AUTH`
to select Pi's native locked credential store. A separate login avoids rotating the host's
refresh token. See [Pi's profile notes](pi/README.md).

## Containerfile

```dockerfile
ARG RUNTIME_IMAGE
FROM ${RUNTIME_IMAGE}
# devbox passes the host's version when version_command finds one; this is the fallback.
ARG HARNESS_VERSION=1.2.3
RUN npm install -g example-agent@${HARNESS_VERSION}
```

- Build `FROM ${RUNTIME_IMAGE}`: the project image plus shared terminal tools (including
  tmux, git and Vim) and the `agent` user (UID 10001, home `/home/agent`). The runtime sets
  `EDITOR=vim` and `VISUAL=vim` for shells and external-editor actions; harnesses do not need
  to install or configure an editor separately. Build steps run as root; the harness runs
  as `agent`.
- Install the harness system-wide. Its home directory is bind-mounted from the host at run
  time, so anything written under `/home/agent` during the build is hidden.
- Pin a default `HARNESS_VERSION` so builds work without the harness on the host.
- For `prepared_dirs`, install into `/opt/devbox/prepared/<dir>` and `chown -R agent:agent`
  it. `ARG PREPARE_REVISION` changes on every explicit `devbox build`: reference it in a step
  to refresh unpinned packages there.
- The base is Debian or Ubuntu (apt, glibc).

## hooks.py

Plain functions, all optional. Raise `ValueError` with a user-facing message to stop a launch.

- `build_files(seed_dir: Path) -> dict[str, str]`: extra build-context files (flat names, no
  leading dot) derived from host config, such as a package list. They land in image layers:
  never return credentials. Skipped with `--no-config`.
- `adapt_seed(state_dir: Path) -> list[str]`: edit the copied config in place, e.g. remove
  settings that name host paths or run host commands. Return a note per change.
- `resume_file(session_id: str, cwd: str) -> str`: where, relative to `/home/agent`, the
  harness expects a session snapshot to find it by ID. Without it, snapshots go to
  `.devbox/resume.jsonl`.

Copy credentials only if the guest can use them without changing the host's copy: a
harness that refreshes tokens in place, like Pi's OAuth login or Claude Code's login, would
invalidate the host's session. Prefer a separate long-lived token passed through `secrets`,
or a separate shared login with a harness that supports locked credential storage.
