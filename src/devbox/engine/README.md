# Container engines

`App` talks to a container engine only through `Engine` in [engine.py](engine.py). Each
backend is a small adapter around one CLI: [apple.py](apple.py) for Apple's `container`
(a VM per container) and [podman.py](podman.py) for rootless Podman (shared-kernel
containers). Use them as worked examples.

## Adding a backend

1. Subclass `Engine` in `devbox/engine/<name>.py`.
2. Register it: add the class to `select_engine` and the platform to `backend_name` in
   engine.py, and the name to the backends `Store` accepts in [state.py](../state.py).
   The backend is chosen by host OS today; a second backend on the same OS would need an
   explicit selection mechanism first.
3. Add `tests/engine/test_<name>.py` (see Testing below).
4. Document installation and isolation in the top-level README.

## Attributes

| Attribute | Meaning |
| --- | --- |
| `name` | Backend name and saved-state namespace (`<state>/<name>/`). Never reuse one: state is not portable between engines. |
| `executable` | CLI that `call` runs. |
| `install_hint` | Error shown when the executable is missing. |
| `isolation` | Noun the agent is told it runs in, e.g. `VM` or `container`. |
| `run_options` | Backend-specific `run` flags placed before the shared ones, e.g. user namespace and capability settings. |

## What the shared methods assume

`run`, `exec`, `connect` and `stop` are implemented in the base class for a Docker-like CLI.
Override any whose syntax differs:

- `run`: `run --detach --init --name <id> <run_options> --cpus N --memory M`, then
  `--label k=v`, `--mount type=bind,source=,target=` and `--env K=V`, then the image.
- `exec`: `exec --user agent [--env NAME ...] [--interactive] <id> <args>`. Variables are
  passed by name, so values stay out of the process list. With `input`, stdin is piped
  (`devbox sync` sends a bundle this way).
- `connect`: replaces the Devbox process with `exec -it --user agent [--env TERM]
  [--workdir DIR] <id> sh -c TERMINAL sh <args>`, so the user's terminal attaches directly.
- `stop`: `stop <id>`.

## Abstract methods

- `require_running()`: check the service is usable, offering to start it if appropriate, and
  reject unsafe configurations (Podman refuses rootful mode and a krun default runtime).
- `image_exists(reference)`: whether a prepared image exists. Raise on errors other than
  "not found"; never treat them as absence.
- `building()`: context manager around a sequence of builds, for engines with a builder
  service (Apple starts it and stops it again if it was not running).
- `build(tag, file, context, args, *, platform, cpus, labels)`: build a Containerfile. The
  labels must land in the image's OCI config, because cleanup reads them from there.
- `images()`: every image, including dangling ones, with their OCI config labels.
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
  by Devbox's ownership labels or exact names.
- **Mount only what `RunSpec` lists**: the instance's clone at the checkout path, its home at
  `/home/agent`. No host home, sockets, devices or host networking.
- **Ownership must work both ways.** The guest's `agent` user (UID 10001) writes the
  bind-mounted clone and home, and the host user must be able to read and delete what it
  writes (Podman maps the host user to 10001 with `keep-id`). Devbox never runs commands as
  guest root, so drop every capability the engine allows.
- **Builds see only their context.** Never add host mounts, secrets or SSH forwarding.
- **Limits.** Apply `cpus` and `memory`; warn if the engine silently ignores them.

## Testing

Tests must not need a running container service. Backend tests replace `subprocess.run`
or `Engine.call` and assert the exact arguments sent, and feed recorded or malformed JSON to
the inventory methods to check they parse it, or fail closed
([test_podman.py](../../../tests/engine/test_podman.py) is the fullest example).
Application tests use `tests/fakes.FakeEngine` and never reach backend code.
