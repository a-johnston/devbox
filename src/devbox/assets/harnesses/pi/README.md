# Pi's shared devbox login

## Storage and first login

Pi instances launched with configuration enabled share a **devbox-only** credential store:

- Host: `$XDG_CONFIG_HOME/devbox/shared/pi/auth/` (default `~/.config/devbox/shared/pi/auth/`).
- Guest: `/home/agent/.devbox/pi-auth/`, selected by `DEVBOX_SHARED_AUTH`.
- Contents: Pi's `auth.json` and temporary `auth.json.lock` directory. No settings or sessions.

The host directory is created privately (`0700`); Pi creates `auth.json` with mode `0600`.
The whole directory is mounted read/write so Pi's native lock acquisition, token refresh,
and credential write operate on the same shared filesystem in every instance. devbox never
reads its contents or copies credentials into images or per-instance homes.

Run `/login` in any guest once. New instances and `devbox fork` reuse that login. Each
instance has its own settings, model configuration, packages, instructions and conversations.
Removing containers or running `cleanup` does not remove shared auth. Changing `--state-dir`
does not select a new identity; `XDG_CONFIG_HOME` selects the shared-state base.

Every participating guest can read and change these credentials, and `/logout` affects all
of them. Revoking the login at the provider invalidates it for all instances too. This is
intentional sharing, not per-container keys or a credential broker. Stop all participating
instances before manually replacing or removing the shared directory.

`--no-config` does not mount or select shared auth. That guest starts with its own empty
credential store under `.pi/agent` and can log in independently.

## Upgrading from copied credentials

1. Stop old Pi instances that were given copies of the host's OAuth credentials. They do not
   acquire this shared store or launcher retroactively and can still invalidate the host login.
2. After updating devbox, run `devbox build pi` in each project whose prepared image you use.
3. Launch a new Pi instance and run `/login` for a fresh devbox-only authorization. Do not
   copy an existing host/old-instance OAuth login into the shared directory.
4. If the host login was already invalidated, log in again on the host after stopping the old
   instances. Host Pi and devbox then refresh separate authorizations.

Old saved homes are left untouched, including any old auth files; deleting an old instance
through `cleanup` removes its saved copies, but not the new shared login.

## Launcher and extension compatibility

`launcher.mjs`, installed as `devbox-pi`, uses the official Pi SDK's
`ModelRuntime.create({ authPath })` and native locked credential storage. It does not implement
its own token refresh, patch Pi internals, or require a Pi fork. It launches Pi's interactive
UI, supports the appended Devbox instructions and snapshot forking, and retains the auth
path when `/new`, `/resume` or `/fork` replaces a session. Standard resource discovery loads
the instance's configured packages and extensions, plus the codemode/tool-search/MCP built-ins.
Project resources remain trust-gated; startup asks before loading unapproved project config,
and `/trust` can save a decision. Project-trust extension hooks are not part of this launcher.

This is an interactive entry point, not a drop-in replacement for every Pi CLI mode.
`devbox-pi --help` lists its supported arguments. The ordinary `pi` executable is preserved
for package management, print/RPC mode and other CLI commands; it uses its normal auth path,
not the shared store. Use `devbox-pi` when manually restarting the shared-auth interactive
harness from a guest shell.

Extensions that directly read or write `<agent-dir>/auth.json` (such as multi-pass's keeper)
bypass the SDK auth path and are **not compatible with this shared login** without adaptation.
Do not add an auth-file symlink as a workaround: an extension could use a different lock.
Pi's own login-success text may still name its default auth path; the launcher prints the
actual credential path at startup, and the SDK writes to that selected path.

## Optional host launcher

Unmodified host Pi continues to use its own login. To opt into the same shared identity,
run this launcher on the host with the Pi SDK installed beside it and set `DEVBOX_SHARED_AUTH`
to the host directory above. For example, from a devbox source checkout:

```sh
launcher="$HOME/.local/share/devbox/pi-launcher"
mkdir -p "$launcher"
npm install --prefix "$launcher" --ignore-scripts @earendil-works/pi-coding-agent@1.3.0
cp src/devbox/assets/harnesses/pi/launcher.mjs "$launcher/launcher.mjs"
DEVBOX_SHARED_AUTH="${XDG_CONFIG_HOME:-$HOME/.config}/devbox/shared/pi/auth" \
  node "$launcher/launcher.mjs"
```

This is optional, manual setup, separate from devbox's Python installation. Keep the SDK
version compatible with the guest and recopy the launcher after updating it. The launcher
is exercised against Pi 1.3.0 (the image fallback) and the development environment's Pi.
**Only do this if you trust guests to write state that a host process consumes**, including
filesystem entries, not just the credential values. There is no filesystem allowlist or
broker protecting a host launcher from malicious changes within that shared directory.

## Tests

`uv run pytest` runs mount/permission/isolation tests and, when Node.js is available, launcher
wiring tests without an installed SDK or container service. To additionally exercise native
SDK login, conversation forking, the interactive login selector in a pseudo-terminal, and
concurrent synthetic OAuth refreshes across four processes:

```sh
DEVBOX_PI_SDK=/absolute/path/to/pi-coding-agent/dist/index.js \
  uv run pytest tests/test_pi_launcher.py
```

These checks use temporary directories and synthetic credentials; they make no provider
requests. Cross-VM filesystem locking/ownership still requires a real backend integration
check, especially for macOS's shared-filesystem implementation.
