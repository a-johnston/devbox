# Phase 3: Engine Interface and Rootless Podman Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `App` and the CLI talk to a small engine interface. Apple `container` (macOS) and rootless Podman (Linux) each implement it, and the right one is chosen automatically.

**Architecture:** `engine.py` keeps the shared pieces: `Container`, a new `RunSpec`, `host_platform`, the mount-flag helper, the `Engine` protocol, a `CliEngine` base class that owns subprocess calls, and `select_engine()`. Apple's code moves from `Engine` into `apple.py`'s `AppleEngine`, along with the builder lifecycle that's now in `App._build`. `podman.py` adds `PodmanEngine`. App tests keep using a `FakeEngine` that subclasses `AppleEngine` and records Apple argv, so they keep checking the app→Apple translation; Podman gets its own translation tests.

**Tech Stack:** Python 3.12+, pytest, uv; Apple `container` 1.5+; Podman ≥ 4.3 (rootless, cgroup v2), on Arch Linux x86_64.

**Spec:** `docs/superpowers/specs/2026-10-21-linux-backend-design.md` (Phase 3)

## Handoff: running this on the Arch PC

Tasks 1–4 are unit-tested and can run on either machine. Task 5 is the integration pass and needs the Arch PC. A session picking this up on the PC should:

1. Read the spec, then this plan. Check the branch's commits and the ledger (`.superpowers/sdd/2026-10-21-phase-3-podman-backend/progress.md`, if the branch was started elsewhere it won't exist: rebuild state from `git log main..podman-backend` and the plan's task commit messages).
2. Install the prerequisites: `sudo pacman -S podman fzf uv git` (Podman pulls in `crun`, `catatonit`, `netavark`, `aardvark-dns`, `passt`/`slirp4netns`). Check that `/etc/subuid` and `/etc/subgid` have an entry for the user (add with `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $USER` if not), then run `podman info` and check `rootless: true` and `cgroupVersion: v2`.
3. Set up the checkout following AGENTS.md (`uv sync --locked`, `uv run prek install`, `uv tool install --editable .`). For Claude, set `CLAUDE_CODE_OAUTH_TOKEN` for agentbox only (README "Harness config and credentials").
4. rouge-legacy also needs cloning on the PC to test its image (Task 5, step 7).

The facts below about Podman come from its documentation, not from running it. Task 5 confirms each one; if one is wrong, fix it under TDD (a failing translation test first) and ledger a ruling.

| Assumption | Used by | Checked in |
|---|---|---|
| `--userns=keep-id:uid=10001,gid=10001` maps the host user to the guest `agent`, so guest-written files are owned by the host user | `PodmanEngine.run` | Task 5 step 3 |
| `podman exec --env NAME` (no value) copies `NAME` from the caller's environment | `PodmanEngine.exec` (secrets) | Task 5 step 4 |
| `podman build --platform linux/<arch>` sets `TARGETARCH` automatically | rouge-legacy Containerfile | Task 5 step 7 |
| `podman volume create --label …` + `podman volume ls --filter label=… --format '{{.Name}}'` | volume cleanup | Task 5 step 5 |
| `podman ps --all --filter label=… --format json` rows have `Names` (list), `State`, `Labels`, `CreatedAt` | `PodmanEngine.containers` | Task 5 step 2 |
| `podman image exists REF` exits 0 if present, 1 if absent, other codes for errors | `PodmanEngine.image_exists` | Task 5 step 2 |
| `--init` works rootless (catatonit) | `PodmanEngine.run` | Task 5 step 2 |

## Global Constraints

- Python 3.12 compatible; run `uv run pytest` after every source change, and `uv run prek run --all-files` (check its exit status, not filtered output) before asking to land.
- Work on branch `podman-backend` in `.worktrees/podman-backend`, created from `main` before the first edit (`git worktree add .worktrees/podman-backend -b podman-backend`, then `uv sync --locked`).
- No new runtime dependencies.
- Unit tests must not need `container` or `podman` installed: mock `subprocess.run`/`call`.
- Ownership: list and remove only containers and volumes carrying `dev.agentbox.managed=1`; never use container-wide `--all` cleanup flags (listing with `ps --all` plus a label filter is fine). Removing a container never deletes saved state.
- The guest `agent` user is UID/GID 10001 (`assets/runtime/Containerfile`).
- Engine selection: `AGENTBOX_ENGINE` = `apple` | `podman` overrides; otherwise `sys.platform` `darwin` → Apple and `linux` → Podman; anything else is an `AgentboxError`.
- Builder memory: env `AGENTBOX_BUILDER_MEMORY`, validated like `config.size`, default `"4G"`, read only by `AppleEngine`. The `[builder]` table is no longer read (leftover tables are ignored).
- `environment.md` describes isolation: `"VM"` for Apple, `"container"` for Podman.
- Out of scope: the krun runtime (phase 4), Docker, Podman on macOS.

## Review Focus

- `agentbox ls` / `kill-all` on Podman when the host has unrelated containers and volumes: only labelled ones are listed or removed (Task 3 tests: filter flags and client-side label check).
- Podman's container states (`exited`, `created`, `paused`) instead of Apple's `stopped`: `attach`/`shell` refuse non-`running` containers, and `rm` accepts any state (Task 3 test maps rows; the CLI already compares to `"running"`).
- `podman image exists` failing for a reason other than "absent" (e.g. a broken storage config, exit 125) surfaces as an error, rather than triggering a surprise rebuild (Task 3 test).
- An unset/invalid `AGENTBOX_ENGINE` or `AGENTBOX_BUILDER_MEMORY` gives a clear error naming the variable (Task 1 and Task 4 tests).
- Removing a Podman container whose volumes were never created (start failed between `volume create` and `run`) still removes the volumes that do exist, without errors (Task 3 test).

---

### Task 1: Builder memory from the environment

**Files:**
- Modify: `src/agentbox/config.py` (remove `builder_memory` field, the `builder` table read, and its `size(...)` argument, lines ~95, ~123, ~148)
- Modify: `src/agentbox/engine.py` (`Engine.build` drops its `memory` parameter and reads `AGENTBOX_BUILDER_MEMORY`), `src/agentbox/app.py` (stop passing `memory=`)
- Modify: `tests/test_config.py` (line 20 tuple, line 112 parametrize case, `test_builder_memory_is_independent`), `tests/test_preparation.py` (`test_explicit_build_rebuilds_with_builder_memory`)
- Modify: `.agentbox.toml` (remove `[builder]`), `README.md` (manifest example, "Image preparation")

**Interfaces:**
- Produces: `builder_memory() -> str` in `agentbox.engine` (Task 2 moves it to `agentbox.apple`): `os.environ.get("AGENTBOX_BUILDER_MEMORY", "4G")`, validated with `config.size(value, "AGENTBOX_BUILDER_MEMORY")`.

- [ ] **Step 1: Write the failing tests**

- `test_config.py`: line 20 becomes `assert (project.cpus, project.memory) == (4, "8G")`; remove the `[builder]` parametrize case; replace `test_builder_memory_is_independent` with:

```python
def test_builder_table_is_ignored(repository: Path) -> None:
    with (repository / ".agentbox.toml").open("a") as stream:
        stream.write('\n[builder]\nmemory = "garbage"\n')
    assert not hasattr(Project.discover(repository), "builder_memory")
```

- `test_preparation.py`: replace `test_explicit_build_rebuilds_with_builder_memory` with a test that sets `AGENTBOX_BUILDER_MEMORY=2G`, runs `app.build(...)`, unsets it, and runs it again. Assert that the `--memory` values of the build calls are `["2G"] * 3 + ["4G"] * 3`, and that both builds return the same image name (builder memory isn't an image variant).
- `test_engine.py`: `AGENTBOX_BUILDER_MEMORY=lots` → `builder_memory()` raises `AgentboxError` matching `AGENTBOX_BUILDER_MEMORY`.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_config.py tests/test_preparation.py tests/test_engine.py -q`
Expected: failures for `test_builder_table_is_ignored` (the garbage value raises), the env-memory test, and an `ImportError` for `builder_memory`.

- [ ] **Step 3: Implement** as described in Files/Interfaces. In the README, remove `[builder]` from the manifest example, and in "Image preparation" add: "On macOS the builder gets `AGENTBOX_BUILDER_MEMORY` (default `4G`)."

- [ ] **Step 4: Run the full suite** — `uv run pytest -q`, all pass.

- [ ] **Step 5: Commit**

```bash
git add src tests .agentbox.toml README.md
git commit -m "Take builder memory from the environment instead of manifests

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Engine interface; Apple behind it (no behaviour change)

**Files:**
- Modify: `src/agentbox/engine.py` (becomes the shared module)
- Create: `src/agentbox/apple.py`
- Modify: `src/agentbox/app.py` (`_build` builder lifecycle, `new`'s `run` call, `mount` moves out, `App.__init__` default)
- Modify: `src/agentbox/cli.py` (`app.engine.call("stop", …)` → `app.engine.stop(…)`)
- Modify: `tests/test_app.py` (`FakeEngine` base class, imports), `tests/test_engine.py` (Apple tests import `AppleEngine`), `tests/test_preparation.py`, `tests/test_cli.py` (imports)

**Interfaces:**
- Consumes: `builder_memory()` from Task 1.
- Produces, in `agentbox.engine`:
  - `MANAGED`, `Container` (unchanged), `host_platform()` (unchanged)
  - `@dataclass(frozen=True) class RunSpec: name: str; image: str; cpus: int; memory: str; labels: dict[str, str]; binds: tuple[tuple[str, str], ...]; volumes: tuple[tuple[str, str], ...]; env: dict[str, str]`. `binds` is (host source, guest target) and `volumes` is (volume name, guest target), in mount order.
  - `mount(source: Path | str, target: str, *, type: str = "bind") -> tuple[str, str]` (moved from `app.py`, same validation)
  - `class Engine(Protocol)` with: `isolation: str`; `require_running() -> None`; `image_exists(reference: str) -> bool`; `building() -> AbstractContextManager[None]`; `build(tag: str, file: Path, context: Path, args: dict[str, str], *, platform: str, cpus: int) -> None`; `run(spec: RunSpec) -> None`; `containers() -> list[Container]`; `exec(id: str, *args: str, user: str = "agent", check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]`; `connect(id: str, *args: str, cwd: str | None = None) -> None`; `stop(id: str) -> None`; `remove(id: str) -> None`
  - `class CliEngine`: `executable: str`, `install_hint: str`, and `call(*args, capture=True, check=True, env=None)`. This is today's `Engine.call`, with the error text built from `install_hint` and `executable` (e.g. `f"{self.executable} {args[0]} failed: …"`).
  - `select_engine() -> Engine` (Task 4 fills it in; in Task 2 it returns `AppleEngine()`).
- Produces, in `agentbox.apple`: `class AppleEngine(CliEngine)` with `isolation = "VM"`. It holds today's methods (`require_running`, `image_exists`, `build`, `builder_running`, `containers`, `exec`, `connect`, `remove`, `volumes`) plus `stop(id)` (`call("stop", id)`), `run(spec)`, and `building()`.

- [ ] **Step 1: Write the failing tests**

`tests/test_engine.py`: change the import to `from agentbox.apple import AppleEngine`, use `AppleEngine()` where `Engine()` was used, and add:

```python
def test_apple_run_translates_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    engine = AppleEngine()
    monkeypatch.setattr(engine, "call", lambda *a, **kw: calls.append(a))
    engine.run(
        RunSpec(
            name="agentbox-1",
            image="img:latest",
            cpus=2,
            memory="4G",
            labels={MANAGED: "1"},
            binds=(("/src", "/src"),),
            volumes=(("agentbox-1-0", "/src/.venv"),),
            env={"A": "b"},
        )
    )
    assert calls == [
        (
            "run",
            "--detach",
            "--init",
            "--name",
            "agentbox-1",
            "--cpus",
            "2",
            "--memory",
            "4G",
            "--label",
            f"{MANAGED}=1",
            "--mount",
            "type=bind,source=/src,target=/src",
            "--mount",
            "type=volume,source=agentbox-1-0,target=/src/.venv",
            "--env",
            "A=b",
            "img:latest",
        )
    ]
```

`tests/test_cli.py`: the existing `test_stop_explicit_id_needs_no_picker` stays as it is (it asserts `engine.calls == [("stop", "agentbox-aabb")]`), and now runs through `AppleEngine.stop`.

`tests/test_app.py`: `class FakeEngine(AppleEngine)`; `from agentbox.engine import Container, mount`; drop `mount` from the `agentbox.app` import.

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest -q`
Expected: collection errors (`No module named 'agentbox.apple'`).

- [ ] **Step 3: Implement**

- Move the Apple methods into `apple.py` unchanged, except: `builder_memory()` (from Task 1) moves to `apple.py` along with `build`; update the `test_engine.py` import, and the `require_running`/install messages stay Apple-specific (`install_hint = "Apple's container CLI is required: https://github.com/apple/container"`).
- `AppleEngine.building()` is a `contextlib.contextmanager`: `started = not self.builder_running()`, then `yield`, and in `finally`, `if started: self.call("builder", "stop", check=False)`. Keep the comment "Stop a builder we start: it holds gigabytes of RAM and launches don't need it."
- `AppleEngine.run(spec)` produces the argv in the test above. The label order is the dict's order, and every mount goes through `mount()`.
- `App._build`: `with self.engine.building():` around the `TemporaryDirectory` block, and calls `build(..., platform=host_platform(), cpus=project.cpus)`.
- `App.new`: build a `RunSpec` (labels `{MANAGED: "1", "dev.agentbox.project": str(project.root), "dev.agentbox.harness": harness.name}`; binds `((str(project.root), str(project.root)), (str(home), "/home/agent"))`; volumes `tuple((f"{id}-{i}", target) for i, target in enumerate(artifacts))`; env `{**env, **git_identity(project.cwd)}`) and call `self.engine.run(spec)`. Order of argv stays as it is today, so the existing app tests keep passing unchanged.
- `App.__init__(self, engine: Engine | None = None, ...)`: `self.engine = engine or select_engine()`.
- `cli.py`: `app.engine.stop(container.id)`.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass, with no edits to existing assertions besides imports and the `FakeEngine` base.

- [ ] **Step 5: Commit**

```bash
git add src tests
git commit -m "Put Apple container behind an engine interface

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: Podman engine

**Files:**
- Create: `src/agentbox/podman.py`
- Create: `tests/test_podman.py`

**Interfaces:**
- Consumes: `CliEngine`, `RunSpec`, `Container`, `MANAGED`, `mount` from Task 2.
- Produces: `class PodmanEngine(CliEngine)` with `executable = "podman"`, `isolation = "container"`, `install_hint = "Podman is required: https://podman.io/docs/installation"`, and the constant `AGENT_USERNS = "keep-id:uid=10001,gid=10001"`.

Command translation (each line is one test in `test_podman.py`, with `call` or `subprocess.run` mocked and the exact argv asserted):

| Method | argv / behaviour |
|---|---|
| `require_running()` | `call("info", "--format", "json", check=False)`; nonzero → `AgentboxError` with `podman info` and its stderr. |
| `image_exists(ref)` | `call("image", "exists", ref, check=False)`; 0 → True, 1 → False, anything else → `AgentboxError("Cannot check prepared image: …")`. |
| `building()` | no-op context manager. |
| `build(tag, file, context, args, *, platform, cpus)` | `("build", "--platform", platform, "--tag", tag, "--file", str(file), *("--build-arg", "K=V")…, str(context))` with `capture=False`. `cpus` is unused. |
| `run(spec)` | First, for each volume: `("volume", "create", "--label", f"{MANAGED}=1", "--label", f"dev.agentbox.instance={spec.name}", name)`. Then `("run", "--detach", "--init", "--name", name, "--userns", AGENT_USERNS, "--cpus", …, "--memory", …, --label…, --mount… (binds, then volumes), --env…, image)`. |
| `containers()` | `call("ps", "--all", "--filter", f"label={MANAGED}=1", "--format", "json")`. Each row becomes `Container(row["Names"][0], row["State"], labels.get("dev.agentbox.project", "?"), labels.get("dev.agentbox.harness", "?"), row.get("CreatedAt", "?"))`, still filtered on `labels.get(MANAGED) == "1"` client-side. Rows with unexpected shapes → `AgentboxError("Unexpected podman JSON")`. |
| `exec(id, *args, user, check, env)` | `("exec", "--user", user, *("--env", NAME)…, id, *args)` with `env` passed to `call`, as Apple does. |
| `connect(id, *args, cwd)` | `os.execvpe(podman, [podman, "exec", "-it", "--user", "agent", *("--workdir", cwd), id, *args], env_without_TMUX)`. |
| `stop(id)` | `("stop", id)`. |
| `remove(id)` | `("stop", id)` with `check=False`, then `("rm", id)`, then `("volume", "ls", "--filter", f"label=dev.agentbox.instance={id}", "--format", "{{.Name}}")`, then `("volume", "rm", name)` for each line printed. |

- [ ] **Step 1: Write the failing tests**, one per row (`test_podman_require_running_reports_info_failure`, `test_podman_image_exists_distinguishes_absent_from_error` with return codes 0/1/125, `test_podman_build_argv`, `test_podman_run_creates_labelled_volumes_then_runs_with_keep_id`, `test_podman_containers_filters_by_label_and_maps_rows` with one unlabelled row and states `running`/`exited`, `test_podman_exec_passes_secret_names_only`, `test_podman_connect_strips_outer_tmux`, `test_podman_remove_deletes_only_labelled_volumes`). For `remove`, also cover a `volume ls` that prints nothing.

- [ ] **Step 2: Run them and confirm they fail** — `uv run pytest tests/test_podman.py -q` → `No module named 'agentbox.podman'`.

- [ ] **Step 3: Implement** `podman.py` to the table. Share the TMUX-stripping logic with Apple by moving it into a `CliEngine.connect_argv`-style helper, if that reads cleaner than duplicating three lines (builder's call).

- [ ] **Step 4: Run the full suite** — `uv run pytest -q`, all pass.

- [ ] **Step 5: Commit**

```bash
git add src/agentbox/podman.py tests/test_podman.py src/agentbox/engine.py
git commit -m "Add a rootless Podman engine

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 4: Engine selection, isolation wording and docs

**Files:**
- Modify: `src/agentbox/engine.py` (`select_engine`)
- Modify: `src/agentbox/app.py` (`environment.md` uses `self.engine.isolation`)
- Modify: `README.md` (intro, Install, Security scope, Development)
- Test: `tests/test_engine.py`, `tests/test_app.py`

- [ ] **Step 1: Write the failing tests**

- `test_select_engine` parametrized over `(sys.platform, AGENTBOX_ENGINE, expected class)`: `("darwin", None, AppleEngine)`, `("linux", None, PodmanEngine)`, `("darwin", "podman", PodmanEngine)`, `("linux", "apple", AppleEngine)`.
- `test_select_engine_rejects_unknown`: `AGENTBOX_ENGINE=docker` → `AgentboxError` matching `AGENTBOX_ENGINE`; `sys.platform = "win32"` with no override → `AgentboxError` matching `win32`.
- `test_app.py`: with a `FakeEngine` whose `isolation = "container"`, `environment.md` contains `"a Linux ARM64 container"` (with `platform.machine` patched to `arm64`). The existing amd64 test keeps "VM" (FakeEngine defaults to Apple's `isolation`).

- [ ] **Step 2: Run them and confirm they fail.**

- [ ] **Step 3: Implement.** `select_engine` imports `apple`/`podman` inside the function, to avoid a circular import. In `environment.md`: `f"..., a Linux {architecture} {self.engine.isolation}, as a non-root user."`.

  README:
  - Intro: "inside lightweight Linux VMs using Apple's `container` on macOS, or rootless Podman containers on Linux."
  - Install: split into macOS (as today) and Arch Linux (`pacman -S podman fzf uv`, subuid/subgid note, no service to start). Note `AGENTBOX_ENGINE` as the override.
  - Security scope: "On macOS each container is its own VM. On Linux, rootless Podman containers share the host kernel but run in your user namespace, with the host user mapped to the guest `agent`; files the agent creates in the checkout are owned by you." Keep the rest.
  - Development: "`uv run pytest` needs neither container service."

- [ ] **Step 4: Run the full suite and the hooks** — `uv run pytest -q && uv run prek run --all-files; echo $?` → all pass, `0`.

- [ ] **Step 5: Commit**

```bash
git add src tests README.md
git commit -m "Select the engine by platform and describe its isolation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 5: Integration pass on the Arch PC

Manual checks with real Podman. If a check fails, add a failing translation test to `tests/test_podman.py` that reproduces the wrong argv or parse, fix it, and ledger a ruling that names the corrected assumption. Run each check from a checkout of this branch with `uv run agentbox …`.

- [ ] **Step 1: Prerequisites.** `podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupsVersion}}'` → `true v2`.
- [ ] **Step 2: Lifecycle.** `uv run agentbox new pi --detach --project .` (this repo) succeeds and prints the start time. `uv run agentbox ls` shows it as `running`. `podman ps --all --filter label=dev.agentbox.managed=1 --format json` matches the fields `containers()` parses. A second `new` prints "Using prepared image" (`image exists` works). `podman exec <id> ps -o pid,comm 1` shows `catatonit` as PID 1.
- [ ] **Step 3: Ownership.** `uv run agentbox shell <id>`, then `id` → `uid=10001(agent)`. `touch probe && mkdir -p .worktrees/x`, then exit. On the host, `stat -c %U probe` prints your username. Same for a file the harness writes under `~/.local/share/agentbox/instances/<id>/home`. Clean up `probe`. If ownership is wrong, check `podman inspect <id> --format '{{.HostConfig.IDMappings}}'` and fix `AGENT_USERNS`.
- [ ] **Step 4: Secrets.** With `CLAUDE_CODE_OAUTH_TOKEN=dummy-value uv run agentbox new claude --detach`, `podman exec --user agent <id> sh -c 'cat /proc/$(pgrep -f "^claude")/environ | tr "\0" "\n" | grep CLAUDE_CODE_OAUTH_TOKEN'` shows `dummy-value`, and `podman inspect <id> | grep -c dummy-value` prints `0` (it isn't in the container config). Remove the container.
- [ ] **Step 5: Volumes and removal.** `podman volume ls --filter label=dev.agentbox.instance=<id>` lists one volume per artifact path. Inside the guest, `.venv` is writable by `agent` and contains no `lost+found`. `uv run agentbox rm <id>` removes the container and exactly those volumes, `podman volume ls` shows no other volumes touched, and `uv run agentbox sessions` still lists its conversations.
- [ ] **Step 6: Attach.** `uv run agentbox attach <id>` attaches to tmux; Ctrl-a d returns to the host shell; `agentbox stop` and then `attach` refuses (the state is `exited`).
- [ ] **Step 7: rouge-legacy.** `uv run agentbox build claude --project ~/godot/rouge-legacy` downloads `…_linux_x86_64.zip` and the checksum passes (`TARGETARCH` set by Podman). In a new container, `godot --headless --version` prints `4.8.beta1.mono.official…`, and `dotnet test` inside the guest passes.
- [ ] **Step 8: Measure (input to phase 4).** Record in the ledger: the start time `new` prints for pi and claude, and `podman stats --no-stream` memory for an idle claude container and for a rouge-legacy container after `godot --headless --import`.
- [ ] **Step 9: Commit any fixes** (each with its test). Then run `uv run prek run --all-files` and check it exits 0.

Ask for landing approval with the ledger's rulings and the Step 8 measurements. Ask about squashing (the branch will have several commits).
