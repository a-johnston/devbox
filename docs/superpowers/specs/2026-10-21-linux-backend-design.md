# Linux hosts and architecture-neutral images

## Intent

Agentbox currently runs only on Apple silicon macOS, through Apple's `container` CLI. Adam wants to
use it on an Arch Linux PC (x86_64) for the same workflows as on the Mac: agent sessions in this
repo and in `~/godot/rouge-legacy`, including headless Godot work and tests. To get there, the
container backend moves behind a small interface, Linux gets a rootless Podman backend, and images
build for whichever architecture the host has. The core should come out the same size or smaller.

## Goals

- `agentbox new/attach/shell/stop/rm/ls/kill-all/sessions/resume/build` behave the same on macOS
  (Apple `container`) and Arch Linux (rootless Podman).
- The same project manifests and Containerfiles work on both hosts, building natively for arm64 on
  the Mac and amd64 on the PC.
- Files the agent writes in the shared checkout and in saved state are owned by the host user on
  both hosts.
- Podman can run containers in libkrun micro-VMs (`krun`), so isolation on Linux can come close to
  the Mac's VM-per-container model. Whether `krun` becomes the default is decided by measurement.
- Harness secrets come from the host environment. Keychain support is removed.

## Non-goals

- Docker, Podman on macOS, Windows, or Intel Macs. The interface shouldn't rule these out, but
  nothing is built or tested for them.
- Cross-architecture builds or emulation. Building for a different architecture than the host can be
  added later with a `--platform` option.
- GPU or display access (running the Godot editor with its GUI) inside containers.
- Publishing multi-arch images to a registry.

## Anti-goals

These would make the work a failure even if it ran:

- Containerfiles duplicated per architecture, or agentbox-specific templating inside them.
- Project-specific knowledge in the core, such as Godot download names.
- Weakening the existing guarantees: no host home mount, no host command or socket bridge, cleanup
  only of containers and volumes carrying our ownership labels (never container-wide `--all`
  cleanup), and container removal never deletes saved conversations.
- Host files created by the guest that end up owned by a subordinate UID (from `/etc/subuid`) the
  host user can't easily edit or delete.
- Making the macOS path worse in order to share code with Linux.
- Tests that need a running container service (other than explicitly invoked integration checks).

## Constraints

- Python 3.12 compatible, developed on 3.15. Standard repo tooling and workflow from `AGENTS.md`.
- Apple `container` 1.5+ stays the macOS backend, with its current behaviour.
- Guest images keep the `agent` user at UID/GID 10001 (`assets/runtime/Containerfile`).
- Existing `.agentbox.toml` manifests keep loading. The `[builder]` table goes away (see phase 3);
  a leftover one is ignored, since manifest parsing already ignores unknown tables.

## Plan: four changes, landed in this order

Each phase is its own branch and lands separately.

### Phase 1: secrets from the environment

Decided: drop the Keychain integration and read declared secrets from the host environment.

- Harness profiles keep `secrets = ["NAME", ...]`. At launch, `App.new` takes each declared name
  that is set and nonempty in `os.environ` (unless `--no-config` is given) and passes it only to
  the harness process, using the existing `exec --env NAME` path. The value never appears in
  command arguments, container configuration, images or saved state. Keep the "Using stored
  credentials" notice, renamed to something like "Forwarding credentials from environment: NAME".
- A secret that isn't set is skipped silently. The harness falls back to its own login, as it does
  today when nothing is stored.
- Remove `keychain.py`, the `agentbox secret` subcommand, `tests/test_secrets.py` (or replace it
  with environment-forwarding tests), and Keychain references in the README and the Claude profile
  comment. Update the README with `claude setup-token`, and suggest exporting the token from your
  shell profile or wrapping the launch with a password-manager CLI (`op run -- agentbox new claude`).
- Builder's call (Claude): remove `.credentials.json` from the Claude profile's `seed_files`. On a
  Linux host that file *is* the host's login, and copying it brings back the token-rotation problem
  the README describes. The environment token replaces it on both hosts.

### Phase 2: build for the host architecture

- `engine.build` stops hardcoding `linux/arm64`. Agentbox works out the host architecture
  (`platform.machine()`: `arm64`/`aarch64` → `arm64`, `x86_64`/`amd64` → `amd64`, anything else is
  an error) and passes `--platform linux/<arch>` explicitly, so builds are deterministic on both
  backends.
- The architecture becomes part of the prepared image's name or variant hash, so a state directory
  shared between machines can't confuse images.
- `environment.md` (written in `App.new`) currently says "a Linux ARM64 VM". It should report the
  real architecture and the backend's isolation ("VM" for Apple and krun, "container" for crun).
  The "Do not assume macOS tools" line becomes "Do not assume host tools".
- Small platform-neutral fixes: the CLI description ("disposable Apple containers") and the fzf
  hint (`brew install fzf`).
- **rouge-legacy** (separate repo, follow its own AGENTS.md workflow): keep one Containerfile.
  Declare `ARG TARGETARCH` with no default (BuildKit and Buildah fill it in), and replace the
  `test "$TARGETARCH" = arm64` guard with a `case` that maps `amd64` → `x86_64` and `arm64` →
  `arm64` and selects the matching SHA-512; any other architecture fails loudly. Godot's names are
  `Godot_v<ver>_mono_linux_<arch>.zip` with the binary at
  `Godot_v<ver>_mono_linux_<arch>/Godot_v<ver>_mono_linux.<arch>`. Take the x86_64 hash from the
  release's `SHA512-SUMS.txt`, never from memory. Check the rest of that Containerfile (apt
  packages, `blender`) on amd64 during phase 3 testing.

### Phase 3: backend interface, Apple and Podman

Decided: rootless Podman on Linux (not LXC/Incus, which has no Containerfile build path). The
builder lifecycle stays inside the Apple backend.

**Interface.** Replace `Engine` with a backend protocol that `App` and the CLI use exclusively. Raw
`engine.call(...)` from `app.py`/`cli.py` (`run`, `stop`, `builder stop`) goes away. Roughly:

- `require_running()`: Apple checks `container system status`; Podman checks that `podman` exists
  and `podman info` succeeds.
- `image_exists(ref)`
- `building(cpus)` (context manager) around a sequence of `build(tag, file, context, args,
  platform)` calls. Apple starts the builder if needed and stops it afterwards only if it started it,
  which is the logic now in `App._build`. Podman does nothing here.
- `run(spec)`, where a `RunSpec` dataclass holds id/name, image, cpus, memory, labels, bind mounts,
  named volumes, and environment. Each backend turns it into its own flags.
- `containers()`, filtered to `dev.agentbox.managed=1`; Podman filters on the server side with
  `--filter label=...`.
- `exec(...)`, `connect(...)`, `stop(id)`, `remove(id)` (the container plus only its own artifact
  volumes).
- An isolation/description string for `environment.md` and `ls`.

**Selection.** Builder's call: choose by `sys.platform` (`darwin` → Apple, `linux` → Podman), with
an `AGENTBOX_BACKEND` environment variable to override it. No new host config file.

**Podman specifics** (verify each against the installed Podman; target ≥ 4.3):

- *UID mapping:* run with `--userns=keep-id:uid=10001,gid=10001`, so the host user appears inside
  the container as `agent`. Files the guest writes in the bind-mounted checkout and in
  `instances/<id>/home` are then owned by the host user. Without this, rootless Podman maps them to
  subordinate UIDs, which is an anti-goal. Possibly `safe.directory '*'` is no longer needed under
  Podman, but keep it, since the Mac still needs it.
- *Volumes:* create artifact volumes with ownership labels (`dev.agentbox.managed=1`,
  `dev.agentbox.instance=<id>`) and delete them by label filter. Keep the existing `chown` /
  `lost+found` step, which is harmless on Podman's directory-backed volumes; fresh volumes are
  still root-owned.
- *Containers:* `podman run --detach --init --name ... --cpus ... --memory ... --label ...
  --mount ...`; `podman ps -a --filter label=dev.agentbox.managed=1 --format json`, mapping
  `Names`, `State`, `Labels` and `CreatedAt` onto `Container`; `podman image exists`; `podman exec
  -it --user agent --workdir ...` for `connect`. Check that `podman exec --env NAME` with no value
  inherits the value from the caller's environment. If it doesn't, find another way that keeps the
  value out of argv.
- *Resources:* `--cpus`/`--memory` are cgroup limits under crun (rootless needs cgroup v2 delegation,
  which Arch's systemd provides by default). Under krun they size the VM (verify in phase 4).
- *Builder memory:* decided: remove the `[builder] memory` manifest field and `Project.builder_memory`.
  Builder memory is a host concern that only the Apple backend has, so it reads an optional
  `AGENTBOX_BUILDER_MEMORY` (validated like other sizes, default `4G`, today's default). Builder CPUs
  keep coming from `container.cpus`. Remove `[builder]` from this repo's `.agentbox.toml` and the
  README example, and from rouge-legacy's manifest along with its phase-2 change.

**Tests.** Keep `App` tests on a fake backend. Give each backend a unit test of how it translates
calls into CLI arguments (in the style of `test_engine.py`, with `subprocess.run` mocked). None of
these need Podman or `container` installed. Optionally add a marked integration check that runs a
real `new --detach` / `exec` / `rm` cycle when invoked explicitly.

**Docs.** README: requirements per platform (Arch: `podman`, `fzf`, `uv`; optionally crun with
libkrun support for krun), and a security section that states each backend's isolation.

### Phase 4: libkrun runtime

Decided: support krun as a Podman setting rather than a separate backend, and decide its default by
measuring.

- Builder's call: a Podman-backend setting `AGENTBOX_PODMAN_RUNTIME` (unset → Podman's default,
  `krun` → `--runtime krun`, or the krun annotation if that's what the installed crun needs). Check
  on Arch which packages provide it (`libkrun`, and a crun built with krun support).
- Check that every phase-3 behaviour works under krun: keep-id ownership on bind mounts (virtiofs),
  named volumes, `exec --env` inheritance, tmux attach/detach, `stop`/`rm`.
- Measure on the PC for crun and krun: `new --detach` start time (agentbox already prints it),
  steady-state host memory for an idle Claude container and for a rouge-legacy container after a
  Godot import, and the speed of a representative rouge-legacy test run. Record the results in this
  document or the README, then pick the default.

## Left to the builder

The exact protocol/class shape and module layout (e.g. `backends/apple.py`, `backends/podman.py`),
the naming of `RunSpec` fields, how `Container.created` is formatted across backends, the wording of
messages, and how to structure the backend tests.
