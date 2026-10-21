# Phase 2: Build for the Host Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Images build natively for the host's architecture (arm64 or amd64). The rouge-legacy Containerfile picks its Godot download from `TARGETARCH`.

**Architecture:** A new `host_platform()` in `engine.py` maps `platform.machine()` to `linux/arm64` or `linux/amd64`. `Engine.build` takes the platform as a parameter instead of hardcoding it, `App` passes `host_platform()`, and the platform becomes part of the prepared image's variant hash. The guest's `environment.md` reports the real architecture. In rouge-legacy, one Containerfile uses a `case` on the build-provided `TARGETARCH`.

**Tech Stack:** Python 3.12+, pytest, uv; Dockerfile syntax (BuildKit/Buildah).

**Spec:** `docs/superpowers/specs/2026-10-21-linux-backend-design.md` (Phase 2, plus the rouge-legacy `[builder]` removal from Phase 3)

## Global Constraints

- Python 3.12 compatible; run `uv run pytest` after every source change and `uv run prek run --all-files` (check its exit status, not filtered output) before asking to land.
- agentbox work happens in `.worktrees/host-arch` on branch `host-arch` (already created; this plan is its first commit).
- rouge-legacy work happens in `~/godot/rouge-legacy/.worktrees/agentbox-arch` on branch `agentbox-arch`. Follow that repo's AGENTS.md: ask before landing, rerun `dotnet test` before `--ff-only`.
- Architecture names: Docker/OCI `amd64`/`arm64`. Godot's are `x86_64`/`arm64`.
- Godot 4.8-beta1 mono SHA-512s, from the release's `SHA512-SUMS.txt`:
  - `x86_64`: `609dacd1b33d2de74eb2e026bb14421d78c96f22d9c48343008f07fe8fe257ae6c161a1f938380e604c6d0fe2e3c93445b659af4f6d25fe98837313cfbf9870a`
  - `arm64`: `d593e5495ef1c50850b0bc6722d7899e2250927663b598bd32d12093cccc3389680452958ef1518031e18fe5140061895d231c13f5b37cd4e0745f2c63efc491` (matches the current file)
- No cross-architecture builds: there is no `--platform` CLI option.

## Review Focus

- An unsupported host (`platform.machine()` returning `riscv64`, `armv7l`, or `""`): a clear `AgentboxError` naming the machine, raised before any build call (Task 1 test).
- Case and spelling variants: `aarch64`, `arm64`, `x86_64`, `AMD64` (Windows-style uppercase) all map correctly (Task 1 parametrized test).
- Images prepared before this change keep working: the variant hash changes once, which triggers one rebuild and no errors (covered by the existing `test_second_new_never_starts_or_checks_builder` staying green).
- The rouge-legacy Containerfile on an unexpected `TARGETARCH`, or with none set: it fails the build with a message, rather than downloading the wrong zip or failing the checksum with a confusing error (Task 3 manual check).
- The prompt sentence in `environment.md` still reads correctly with each architecture (Task 1 test asserts the full sentence for amd64).

---

### Task 1: Build for the host architecture

**Files:**
- Modify: `src/agentbox/engine.py` (new `host_platform`; `Engine.build` signature and `--platform` value, lines ~69-90)
- Modify: `src/agentbox/app.py` (`_prepared_image` inputs, line ~80; `self.engine.build(...)` call in `_build`, line ~137; `environment.md` text in `new`, lines ~211-218)
- Modify: `tests/test_app.py` (`FakeEngine` is unaffected; new assertions), `tests/test_engine.py`, `tests/test_preparation.py`

**Interfaces:**
- Produces: `host_platform() -> str` in `agentbox.engine`, returning `"linux/arm64"` or `"linux/amd64"` and raising `AgentboxError` otherwise.
- Produces: `Engine.build(self, tag: str, file: Path, context: Path, args: dict[str, str], *, platform: str, memory: str, cpus: int) -> None`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_engine.py`:

```python
@pytest.mark.parametrize(
    "machine,expected",
    [
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
        ("x86_64", "linux/amd64"),
        ("AMD64", "linux/amd64"),
    ],
)
def test_host_platform(monkeypatch: pytest.MonkeyPatch, machine: str, expected: str) -> None:
    monkeypatch.setattr("platform.machine", lambda: machine)
    assert host_platform() == expected


@pytest.mark.parametrize("machine", ["riscv64", "armv7l", ""])
def test_unsupported_host_platform(monkeypatch: pytest.MonkeyPatch, machine: str) -> None:
    monkeypatch.setattr("platform.machine", lambda: machine)
    with pytest.raises(AgentboxError, match="Unsupported host architecture"):
        host_platform()
```

In `tests/test_app.py`, a new test:

```python
def test_builds_and_describes_the_host_architecture(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    builds = [call for call in engine.calls if call[0] == "build"]
    assert [b[b.index("--platform") + 1] for b in builds] == ["linux/amd64"] * 3
    text = (app.store.directory(id) / "home/.agentbox/environment.md").read_text()
    assert f"agentbox instance {id}, a Linux AMD64 VM, as a non-root user." in text
    assert "Do not assume host tools" in text
```

In `tests/test_preparation.py`, a new test:

```python
def test_architecture_is_an_image_variant(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    arm = app._prepared_image(project, harness, {}, {})
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert app._prepared_image(project, harness, {}, {}) != arm
```

Also add a test that an unsupported host fails before anything is built:

```python
def test_unsupported_host_fails_before_building(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    engine = FakeEngine()
    with pytest.raises(AgentboxError, match="riscv64"):
        App(engine, Store(tmp_path / "state")).new(project, harness, no_config=True)
    assert not any(call[0] == "build" for call in engine.calls)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_engine.py tests/test_app.py tests/test_preparation.py -q`
Expected: collection error, `ImportError: cannot import name 'host_platform'` (once `host_platform` is imported in `test_engine.py`).

- [ ] **Step 3: Implement**

- `host_platform()`: `platform.machine().lower()`; `{"arm64", "aarch64"}` → `"linux/arm64"`, `{"x86_64", "amd64"}` → `"linux/amd64"`; otherwise `AgentboxError(f"Unsupported host architecture: {machine!r}")`. Calling `platform.machine()` through the module (`import platform`) lets the tests' monkeypatch take effect.
- `Engine.build`: add the keyword-only `platform` and pass it to `--platform`.
- `App._prepared_image`: add `"platform": host_platform()` to the JSON `inputs`. Because `new` calls `_prepared_image` before any build, an unsupported host fails before anything is built.
- `App._build`: pass `platform=host_platform()` to `self.engine.build`.
- `environment.md`: `f"... {id}, a Linux {host_platform().removeprefix('linux/').upper()} VM, ..."`, and change `"Do not assume macOS tools or the host editor are available."` to `"Do not assume host tools or the host editor are available."` "VM" stays until phase 3 makes the description depend on the backend.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src tests
git commit -m "Build images for the host architecture

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 2: Platform-neutral CLI wording

**Files:**
- Modify: `src/agentbox/cli.py` (fzf message line ~38, parser description line ~90)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_cli_text_is_platform_neutral() -> None:
    text = parser().format_help()
    assert "Apple" not in text
    assert "Coding harnesses in disposable containers" in text


def test_missing_fzf_hint_is_platform_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("agentbox.cli.shutil.which", lambda _: None)
    with pytest.raises(AgentboxError, match="requires fzf") as error:
        select(["first"], "Harnesses", str)
    assert "brew" not in str(error.value)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `uv run pytest tests/test_cli.py -q -k platform_neutral`
Expected: 2 failed.

- [ ] **Step 3: Implement**

Description: `"Coding harnesses in disposable containers"`. fzf message: `"Interactive selection requires fzf, or supply an explicit argument"`.

- [ ] **Step 4: Run the full suite and hooks**

Run: `uv run pytest && uv run prek run --all-files; echo $?`
Expected: all pass, and the final line is `0`.

- [ ] **Step 5: Commit**

```bash
git add src/agentbox/cli.py tests/test_cli.py
git commit -m "Remove macOS-specific wording from the CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 3: rouge-legacy Containerfile selects Godot by architecture

This task is in the other repository. It's a Containerfile/manifest change with no unit tests, so it's verified by building.

**Files (in `~/godot/rouge-legacy`):**
- Modify: `.agentbox/Containerfile`
- Modify: `.agentbox.toml` (remove the `[builder]` table and its comment)

- [ ] **Step 1: Create the worktree**

```bash
cd ~/godot/rouge-legacy && git worktree add .worktrees/agentbox-arch -b agentbox-arch
```

- [ ] **Step 2: Edit the Containerfile**

- Replace `ARG TARGETARCH=arm64` with `ARG TARGETARCH` (no default), and keep `GODOT_VERSION`. Remove `ARG GODOT_SHA512`: the hashes go into the `case`.
- Remove the `test "$TARGETARCH" = arm64 &&` prefix from the apt `RUN`.
- In the download `RUN`, start with:

  ```sh
  case "$TARGETARCH" in \
      amd64) arch=x86_64; sha=609dacd1…870a ;; \
      arm64) arch=arm64; sha=d593e549…c491 ;; \
      *) echo "Unsupported TARGETARCH: '${TARGETARCH}'" >&2; exit 1 ;; \
  esac \
  ```

  (Use the full hashes from Global Constraints.) Then use `${arch}` in the zip URL (`Godot_v${GODOT_VERSION}_mono_linux_${arch}.zip`), `$sha` in the `sha512sum` line, and in the symlink use `Godot_v${GODOT_VERSION}_mono_linux_${arch}/Godot_v${GODOT_VERSION}_mono_linux.${arch}`.
- Add a one-line comment above the `case`: TARGETARCH is supplied by the builder (BuildKit/Buildah); Godot names architectures differently.

- [ ] **Step 3: Edit the manifest**

Remove the `[builder]` table and its comment from `.agentbox.toml`. agentbox's default builder memory (`4G`) equals the current value, so behaviour doesn't change.

- [ ] **Step 4: Verify the native build**

Run, from the rouge-legacy worktree: `container system start; agentbox build claude --project .`
Expected: the project layer builds and downloads `..._linux_arm64.zip`; the checksum passes; `Environment prepared`.

- [ ] **Step 5: Verify the amd64 branch and the failure path**

Run (project layer only, bypassing agentbox):
`container build --platform linux/amd64 --file .agentbox/Containerfile .agentbox`
Expected: it downloads `..._linux_x86_64.zip` and the checksum passes. If Apple `container` can't build amd64 on this Mac, record that and leave the amd64 check for the Arch PC in phase 3.

Run: `container build --platform linux/arm64 --build-arg TARGETARCH=riscv64 --file .agentbox/Containerfile .agentbox`
Expected: it fails with `Unsupported TARGETARCH: 'riscv64'`.

- [ ] **Step 6: Commit (rouge-legacy)**

```bash
git add .agentbox/Containerfile .agentbox.toml
git commit -m "Select the Godot build from the container's target architecture

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Ask for landing approval for both repositories together. rouge-legacy has one commit, so don't ask about squashing there; for agentbox's `host-arch`, ask about squashing its plan and implementation commits.
