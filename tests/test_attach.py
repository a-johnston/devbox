import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from devbox.app import App
from devbox.config import ASSETS, Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine, FakeRepository


class RecoverableEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.alive = False
        self.fail_start = False
        self.starts = 0

    def exec(self, id: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = super().exec(id, *args, **kwargs)
        if args[:3] == ("bash", "/home/agent/.devbox/harness.sh", "alive"):
            return subprocess.CompletedProcess(args, 0 if self.alive else 1, "", "")
        if args[:3] == ("bash", "/home/agent/.devbox/harness.sh", "start"):
            self.starts += 1
            if self.fail_start:
                raise DevboxError("continuation failed")
            self.alive = True
        return result


def instance(
    project: Project, harness: Harness, tmp_path: Path, *, no_config: bool = True
) -> tuple[App, RecoverableEngine, Container]:
    engine = RecoverableEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())
    id = app.new(project, harness, no_config=no_config)
    engine.execs.clear()
    return app, engine, Container(id, "running", "project", harness.name, "now")


def transcript(app: App, container: Container, name: str, modified: int) -> Path:
    metadata = app.store.metadata(container.id)
    path = app.store.directory(container.id) / "home" / metadata["session_dir"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session", "id": path.stem}) + "\n")
    os.utime(path, ns=(modified, modified))
    return path


def test_live_harness_is_only_attached(project: Project, harness: Harness, tmp_path: Path) -> None:
    app, engine, container = instance(project, harness, tmp_path)
    app.attach(container)
    assert engine.starts == 1
    assert len(engine.execs) == 1 and engine.execs[0].args[-1] == "alive"
    assert engine.connections == [(container.id, ("tmux", "-u", "attach-session", "-t", "agent"), None)]


@pytest.mark.parametrize("kind", ["pi", "claude"])
def test_recovery_continues_latest_local_session_without_forking(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    kind: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = replace(Harness.load(ASSETS / "harnesses" / kind), version_command=(), seed_dir=harness.seed_dir)
    app, engine, container = instance(project, profile, tmp_path)
    transcript(app, container, "project/older.jsonl", 10)
    latest = transcript(app, container, "project/latest.jsonl", 20)
    # Neither another instance's newer conversation nor an invalid local file wins.
    other = app.new(project, profile, no_config=True)
    transcript(app, replace(container, id=other), "project/unrelated.jsonl", 100)
    invalid = transcript(app, container, "project/invalid.jsonl", 200)
    invalid.write_text("not a transcript\n")
    engine.alive = False
    before = latest.read_bytes()
    dirty = app.store.directory(container.id) / "repo/dirty.txt"
    dirty.write_text("uncommitted work")
    app.attach(container)
    # There are two starts in execs: the other instance's initial launch, then recovery.
    start = [r for r in engine.execs if r.id == container.id and r.args[2:3] == ("start",)][0]
    home = app.store.directory(container.id) / "home"
    expected = ("--session", f"/home/agent/{latest.relative_to(home)}") if kind == "pi" else ("--resume", "latest")
    assert start.args[-2:] == expected
    assert "--fork" not in start.args and "--fork-session" not in start.args
    assert dirty.read_text() == "uncommitted work" and latest.read_bytes() == before
    assert len(engine.runs) == 2  # Recovery did not create a container or rebuild.
    assert len(engine.builds) == 3
    assert "continuing session latest" in capsys.readouterr().out


def test_recovery_without_history_starts_fresh_in_same_instance(
    project: Project, harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app, engine, container = instance(project, harness, tmp_path)
    engine.alive = False
    app.attach(container)
    start = next(r for r in engine.execs if r.args[2:3] == ("start",))
    assert start.args[5:] == harness.command
    assert len(engine.runs) == 1 and engine.starts == 2
    assert "starting a new conversation" in capsys.readouterr().out


@pytest.mark.parametrize("no_config", [False, True])
def test_recovery_preserves_policy_and_reacquires_secrets(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_config: bool,
) -> None:
    harness = replace(harness, secrets=("TEST_TOKEN",))
    monkeypatch.setenv("TEST_TOKEN", "original-secret")
    app, engine, container = instance(project, harness, tmp_path, no_config=no_config)
    transcript(app, container, "session.jsonl", 10)
    # Neither a changed profile nor the original host project is needed to recover.
    monkeypatch.setenv("TEST_TOKEN", "replacement-secret")
    shutil.rmtree(project.root)
    engine.alive = False
    app.attach(container)
    start = next(r for r in engine.execs if r.args[2:3] == ("start",))
    assert start.env == ({} if no_config else {"TEST_TOKEN": "replacement-secret"})
    assert start.args[3] == str(project.workdir)
    assert start.args[4] == ("0" if no_config else "1")
    assert "replacement-secret" not in str(start.args)
    metadata = app.store.metadata(container.id)
    assert metadata["launch"]["no_config"] == no_config
    assert "original-secret" not in json.dumps(metadata)
    assert "replacement-secret" not in json.dumps(metadata)


def test_concurrent_attaches_only_restart_once(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, engine, container = instance(project, harness, tmp_path)
    engine.alive = False
    execute = engine.exec

    def delayed_check(id: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = execute(id, *args, **kwargs)
        if args[-1] == "alive":
            time.sleep(0.02)  # Both callers would observe a dead harness without the lock.
        return result

    monkeypatch.setattr(engine, "exec", delayed_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: app.attach(container), range(2)))
    assert engine.starts == 2  # Initial launch and exactly one recovery.
    assert len(engine.connections) == 2


def test_failed_continuation_is_not_replaced_by_a_fresh_conversation(
    project: Project,
    harness: Harness,
    tmp_path: Path,
) -> None:
    app, engine, container = instance(project, harness, tmp_path)
    path = transcript(app, container, "session.jsonl", 10)
    original = path.read_bytes()
    engine.alive = False
    engine.fail_start = True
    with pytest.raises(DevboxError, match="continuation failed"):
        app.attach(container)
    assert path.read_bytes() == original
    assert not engine.connections and not engine.removed
    assert engine.starts == 2


def test_no_in_place_support_keeps_history_and_existing_panes(
    project: Project,
    harness: Harness,
    tmp_path: Path,
) -> None:
    app, engine, container = instance(project, replace(harness, continue_args=()), tmp_path)
    transcript(app, container, "session.jsonl", 10)
    engine.alive = False
    with pytest.raises(DevboxError, match="cannot continue.*devbox fork"):
        app.attach(container)
    assert len(engine.execs) == 1 and not engine.connections


def test_fork_snapshot_is_recoverable_before_harness_writes_a_transcript(
    project: Project,
    harness: Harness,
    tmp_path: Path,
) -> None:
    app, engine, container = instance(project, harness, tmp_path)
    transcript(app, container, "original.jsonl", 10)
    (session,) = app.store.sessions(instance=container.id)
    fork = app.new(project, harness, session=session, no_config=True, from_instance=True)
    engine.execs.clear()
    engine.alive = False
    app.attach(replace(container, id=fork))
    start = next(r for r in engine.execs if r.args[2:3] == ("start",))
    assert start.args[-2:] == ("--session", "/home/agent/.devbox/resume.jsonl")


def test_legacy_instance_has_actionable_recovery_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(FakeEngine(), Store(tmp_path))
    id, directory = app.store.create()
    (directory / "metadata.json").write_text("{}")
    monkeypatch.setattr(app.engine, "exec", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""))
    with pytest.raises(DevboxError, match="older instance.*saved launch policy"):
        app.attach(Container(id, "running", "project", "pi", "now"))


@pytest.mark.parametrize("missing", [False, True])
def test_invalid_fork_snapshot_is_not_silently_replaced(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    missing: bool,
) -> None:
    app, engine, container = instance(project, harness, tmp_path)
    transcript(app, container, "original.jsonl", 10)
    (session,) = app.store.sessions(instance=container.id)
    fork = app.new(project, harness, session=session, no_config=True)
    snapshot = app.store.directory(fork) / "home/.devbox/resume.jsonl"
    if missing:
        snapshot.unlink()
    else:
        snapshot.write_text("invalid transcript")
    engine.execs.clear()
    engine.alive = False
    with pytest.raises(DevboxError, match="Cannot read the saved conversation"):
        app.attach(replace(container, id=fork))
    assert len(engine.execs) == 1 and not engine.connections


def test_continue_args_require_an_explicit_session_placeholder(
    harness: Harness,
    tmp_path: Path,
) -> None:
    profile = (harness.directory / "profile.toml").read_text()
    (tmp_path / "Containerfile").touch()
    (tmp_path / "profile.toml").write_text(
        profile.replace('continue_args = ["--session", "{session}"]', 'continue_args = ["--continue"]')
    )
    with pytest.raises(DevboxError, match="continue_args"):
        Harness.load(tmp_path)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists() or not shutil.which("tmux"), reason="Linux tmux check")
def test_guest_launcher_detects_dead_panes_and_refreshes_tmux_environment(tmp_path: Path) -> None:
    # A private tmux server exercises guest shell code without any container service.
    script = ASSETS / "runtime/harness.sh"
    env = {**os.environ, "TMUX_TMPDIR": str(tmp_path), "TEST_TOKEN": "old"}
    env.pop("TMUX", None)

    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, env=env, check=check, capture_output=True, text=True)

    def wait_for(predicate) -> None:
        for _ in range(100):
            if predicate():
                return
            time.sleep(0.02)
        pytest.fail("guest process did not reach expected state")

    def alive() -> bool:
        return run("bash", str(script), "alive", check=False).returncode == 0

    try:
        assert not alive()
        # Keep a shell in a different session so the server retains its old environment.
        run("tmux", "-f", "/dev/null", "new-session", "-d", "-s", "inspection", "sleep", "60")
        env["TEST_TOKEN"] = "new"
        output = tmp_path / "observed-token"
        command = ("bash", "-c", 'printf %s "$TEST_TOKEN" > "$1"; sleep 60', "--", str(output))
        run("bash", str(script), "start", str(tmp_path), "1", "TEST_TOKEN", *command)
        wait_for(alive)
        wait_for(lambda: output.exists() and output.read_text() == "new")
        # A shell in another pane must not hide the healthy harness.
        run("tmux", "split-window", "-h", "-t", "agent:0.0", "sleep", "60")
        assert alive()
        run("tmux", "respawn-pane", "-k", "-t", "agent:0.0", "sleep", "60")
        wait_for(lambda: not alive())  # A live replacement shell/process is not the harness.
        run("tmux", "set-option", "-t", "agent", "remain-on-exit", "on")
        run("tmux", "respawn-pane", "-k", "-t", "agent:0.0", "false")
        wait_for(lambda: run("tmux", "display-message", "-p", "-t", "agent:0.0", "#{pane_dead}").stdout.strip() == "1")
        assert not alive()
        run("tmux", "kill-session", "-t", "agent")
        # Unsetting a declared secret also masks the stale server environment.
        del env["TEST_TOKEN"]
        run("bash", str(script), "start", str(tmp_path), "1", "TEST_TOKEN", *command)
        wait_for(alive)
        wait_for(lambda: output.read_text() == "")
    finally:
        run("tmux", "kill-server", check=False)
