import json
import subprocess
from pathlib import Path

import pytest

from agentbox.app import App, accent, mount, tmux_config
from agentbox.config import Harness, Project
from agentbox.engine import Container, Engine
from agentbox.errors import AgentboxError
from agentbox.state import Session, Store


class FakeEngine(Engine):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, ...]] = []
        self.connections: list[tuple[str, ...]] = []
        self.rows: list[Container] = []
        self.images: set[str] = set()
        self.builder_state = "stopped"

    def require_running(self) -> None:
        pass

    def call(
        self, *args: str, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[:2] == ("image", "inspect") and args[2] not in self.images:
            return subprocess.CompletedProcess(args, 1, "", f"Error: image not found: {args[2]}")
        if args[:2] == ("builder", "stop"):
            self.builder_state = "stopped"
        if args[0] == "build":
            self.builder_state = "running"
            self.images.add(args[args.index("--tag") + 1])
        if args[:2] == ("volume", "list"):
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[:2] == ("builder", "status"):
            status = [{"status": {"state": self.builder_state}}]
            return subprocess.CompletedProcess(args, 0, json.dumps(status), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def containers(self) -> list[Container]:
        return self.rows

    def connect(self, id: str, *args: str, cwd: str | None = None) -> None:
        self.connections.append((id, *args))


def test_new_composes_images_and_mounts_only_selected_paths(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    builds = [call for call in engine.calls if call[0] == "build"]
    assert len(builds) == 3
    run = next(call for call in engine.calls if call[0] == "run")
    mounts = [run[i + 1] for i, value in enumerate(run) if value == "--mount"]
    assert mounts[:2] == [
        f"type=bind,source={project.root},target={project.root}",
        f"type=bind,source={app.store.directory(id) / 'home'},target=/home/agent",
    ]
    assert mounts[2:] == [
        f"type=volume,source={id}-{i},target={project.root / artifact}"
        for i, artifact in enumerate((".godot", "tests/bin", "tests/obj"))
    ]
    assert not any(".ssh" in arg or arg == "--ssh" for arg in run)
    chown = next(call for call in engine.calls if "chown" in " ".join(call))
    assert chown[:4] == ("exec", "--user", "root", id)
    assert chown[-3:] == tuple(str(project.root / a) for a in (".godot", "tests/bin", "tests/obj"))
    restore = next(call for call in engine.calls if "git ls-files" in " ".join(call))
    assert restore[:4] == ("exec", "--user", "agent", id)
    assert restore[-4:] == (str(project.root), ".godot", "tests/bin", "tests/obj")
    startup = engine.calls[-1]
    assert startup[:4] == ("exec", "--user", "agent", id)
    assert startup[4:15] == (
        "tmux",
        "-u",
        "-f",
        "/home/agent/.agentbox/tmux.conf",
        "new-session",
        "-d",
        "-s",
        "agent",
        "-c",
        str(project.cwd),
        "--",
    )
    assert list(startup[15:]) == list(harness.command)
    home = app.store.directory(id) / "home"
    assert (home / ".pi/agent/npm").readlink() == Path("/opt/agentbox/prepared/.pi/agent/npm")
    assert "prefix C-a" in (home / ".agentbox/tmux.conf").read_text()


def test_git_identity_is_passed_as_environment(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    for key, value in (("user.name", "Ada"), ("user.email", "ada@example.invalid")):
        subprocess.run(["git", "-C", str(project.root), "config", key, value], check=True)
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, harness, no_config=True)
    run = next(call for call in engine.calls if call[0] == "run")
    assert "GIT_AUTHOR_NAME=Ada" in run
    assert "GIT_COMMITTER_NAME=Ada" in run
    assert "GIT_AUTHOR_EMAIL=ada@example.invalid" in run
    assert not any("credential" in arg for arg in run)


def test_resume_snapshots_and_forks(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    original_id, original = app.store.create()
    (original / "metadata.json").write_text("{}")
    source = original / "home/source.jsonl"
    source.write_text('{"type":"session","id":"original"}\n')
    session = Session(f"{original_id}/original", original_id, "pi", str(project.cwd), source)
    id = app.new(project, harness, no_config=True, session=session)
    home = app.store.directory(id) / "home"
    assert (home / ".agentbox/resume.jsonl").read_text() == source.read_text()
    assert engine.calls[-1][-2:] == ("--fork", "/home/agent/.agentbox/resume.jsonl")


def test_failed_start_removes_vm_and_volumes_but_retains_state(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    call = engine.call

    def fail_tmux(*args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        if "tmux" in args:
            raise AgentboxError("startup failed")
        return call(*args, **kwargs)

    monkeypatch.setattr(engine, "call", fail_tmux)
    with pytest.raises(AgentboxError, match="startup failed"):
        app.new(project, harness, no_config=True)
    assert [c[0] for c in engine.calls[-3:]] == ["stop", "delete", "volume"]
    assert len(list(app.store.instances.glob("*/metadata.json"))) == 1


def test_attach_connects_directly_to_tmux(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path))
    container = Container("ours", "running", "project", "pi", "now")
    app.attach(container)
    assert engine.connections == [("ours", "tmux", "-u", "attach-session", "-t", "agent")]


def test_kill_all_retains_state(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [Container("ours", "running", "project", "pi", "now")]
    app = App(engine, Store(tmp_path))
    id, directory = app.store.create()
    (directory / "metadata.json").write_text("{}")
    assert app.kill_all() == 1
    assert (app.store.directory(id) / "metadata.json").exists()
    assert engine.calls[:2] == [("stop", "ours"), ("delete", "ours")]


def test_accent_is_stable_and_status_cannot_inject_tmux_commands() -> None:
    assert accent("id") == accent("id")
    assert len({accent(str(i)) for i in range(30)}) > 4
    config = tmux_config("id", "#(touch /host/pwned)'", "pi")
    assert "#(touch" not in config
    assert "prefix C-a" in config
    assert "status-style" in config
    assert "' p | pi | id '" in tmux_config("id", "p", "pi")


@pytest.mark.parametrize("path", ["/a,b", "/a\nb"])
def test_mount_rejects_cli_delimiters(path: str) -> None:
    with pytest.raises(AgentboxError):
        mount(Path(path), "/workspace")
