# Container engines

`App` talks to a container engine through the [`Engine`](engine.py) interface. Each backend is a small adapter
around its engine: [apple.py](apple.py) for Apple's `container` and [podman.py](podman.py) for rootless Podman.

## Adding a backend

1. Implement a new `Engine` subclass in `devbox/engine/<name>.py`.
2. Add the class to `select_engine` and the platform to `backend_name` in `engine.py`.
3. Add tests to `tests/engine/test_<name>.py` and update docs

## Attributes

- `name`: Backend name and saved-state namespace (`<state>/<name>/`)
- `executable`: CLI that `call` runs
- `install_hint`: Error shown when the executable is missing
- `supports_root`: Defaults to false. Apple and rootless Podman opt in to instance-local sudo; this is a
  capability declaration, **not** a promise of VM isolation. New backends must document their isolation boundary.

## Methods

The base class provides `run`, `exec` and `connect` implementations for a Docker-like CLI.
Override any whose syntax differs:

- `run_options(spec)`: Backend-specific `run` flags placed before the shared ones, including the instance's
  explicit `spec.allow_root` privilege policy. Compute these per launch; never mutate backend-wide flags.
- `run`: `run --detach --init --name <id> <run_options(spec)> --cpus N --memory M`, then
  `--label k=v`, `--mount type=bind,source=,target=` and `--env K=V`, then the image.
- `exec`: `exec --user agent [--env NAME ...] [--interactive] <id> <args>`. Variables are
  passed by name, so values stay out of the process list. With `input`, stdin is piped
  (`devbox sync` sends a bundle this way).
- `require_root_support`: reject an unsupported `--allow-root` before App builds or creates state.
- `enable_root`: recheck support and pipe the host's `runtime/allow-root.sh` into a root exec before starting
  the harness. This atomically installs a validated, root-owned sudoers policy in the instance's writable
  layer, never a bind mount or reusable image, then verify `sudo -n` works as `agent`. Ordinary exec, pipe
  and connect continue to use `agent`.
- `connect`: replaces the Devbox process with `exec -it --user agent [--env TERM]
  [--workdir DIR] <id> sh -c TERMINAL sh <args>`, so the user's terminal attaches directly.

## Abstract methods

- `require_running()`: check the service is usable, offering to start it if appropriate, and
  reject unsafe configurations (Podman refuses rootful mode and a krun default runtime).
- `image_exists(reference)`: whether a prepared image exists. Raise on errors other than
  "not found"; never treat them as absence.
- `building()`: context manager around a sequence of builds, for engines with a builder
  service (Apple starts it and stops it again if it was not running).
- `build(tag, file, context, args, *, platform, cpus, labels)`: build a Containerfile. The
  labels must land in the image's OCI config, because cleanup reads them from there.
- `images()`: every image, including dangling ones, with their OCI config labels and full
  size in bytes (`None` when unavailable). Size estimates may include shared layers; they
  are not exclusive disk usage. All references to the same image must have the same ID.
- `container_images()`: references and IDs used by all containers, running or stopped,
  whoever owns them.
- `remove_image(reference)`: delete exactly that reference, without force or pruning.
- `container_ids()`: IDs of all containers, regardless of labels; saved state for any of
  them is protected from cleanup.
- `containers()`: only containers labelled `dev.devbox.managed=1`.
- `remove(id)`: delete the container, even if it never started.

## Rules

- **Fail closed.** If CLI output is not in the expected shape, raise `DevboxError` rather than
  returning a partial list: cleanup decides what to delete from these inventories.
- **Never clean up by wildcard.** No `prune`, global `--all` deletion or force flags; select
  by devbox's ownership labels or exact names.
- **Mount only what `RunSpec` lists**: the instance's clone at the checkout path, its home at
  `/home/agent`, and any explicitly declared, narrow shared harness state directories. No host
  home or full harness config directory, sockets, devices or host networking. Shared-directory
  mounts must preserve atomic directory creation/removal and mtime updates for credential locks.
- **Ownership must work both ways.** The guest's `agent` user (UID 10001) writes the
  bind-mounted clone and home, and the host user must be able to read and delete what it
  writes (Podman maps the host user to 10001 with `keep-id`). Ordinary commands run as `agent`.
  Podman drops all capabilities and sets no-new-privileges by default. Explicit `--allow-root` is the only
  Devbox-provided path to passwordless sudo. For opted-in Podman instances only, disable no-new-privileges
  and add the bounded package-management capabilities described below. Guest root can change ownership or
  permissions on mounted state, so warn users about host-side access risks. Never add privileged host flags.
- **Builds see only their context.** Never add host mounts, secrets or SSH forwarding.
- **Limits.** Apply `cpus` and `memory`; warn if the engine silently ignores them.

## Rootless Podman guest-root policy

`RunSpec.allow_root` defaults to false. With it enabled, Podman retains the `keep-id:uid=10001,gid=10001`
namespace, mounts, resource limits and default seccomp/LSM confinement. It starts from `--cap-drop all` and
adds only `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `FSETID`, `SETGID`, `SETUID` and `SETFCAP` for sudo, package-file
ownership/modes and package scripts that switch users. File capabilities remain limited by the bounding
set at exec; setting one on a file cannot grant a capability outside that set.

Set `--security-opt no-new-privileges=false` explicitly for opted-in instances, including overriding a true
containers.conf default. Otherwise setuid sudo cannot work. Do not grant `SYS_ADMIN`, network administration
or device-creation capabilities, use `--privileged`, unconfine seccomp/LSMs, mount runtime sockets, or change
the user namespace to the host's. Software needing operations beyond this policy may fail: prefer installing
it at build time rather than silently widening the runtime policy.

This is a **shared-host-kernel** container. Guest root is mapped into subordinate host IDs, not host UID 0,
but can act as the mapped `agent` user and damage its mounted files. The backend warns about the reduced
hardening, and preflight still rejects rootful Podman and krun. A future krun implementation may become the
recommended guest-root mode only when both run and exec reliably execute inside the VM; its current exec
behavior ([crun#2090](https://github.com/containers/crun/issues/2090)) is not VM isolation.

## Testing

Tests must not need a running container service. Backend tests replace `subprocess.run`
or `Engine.call` and assert the exact arguments sent, and feed recorded or malformed JSON to
the inventory methods to check they parse it, or fail closed
([test_podman.py](../../../tests/engine/test_podman.py) is the fullest example).
Application tests use `tests/fakes.FakeEngine` and never reach backend code.
