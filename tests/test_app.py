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
        if args[:2] == ("builder", "start"):
            self.builder_state = "running"
        if args[:2] == ("builder", "stop"):
            self.builder_state = "stopped"
        if args[0] == "build":
            self.images.add(args[args.index("--tag") + 1])
        output = (
            json.dumps(
                [
                    {
                        "id": "buildkit",
                        "configuration": {
                            "labels": {"com.apple.container.resource.role": "builder"}
                        },
                        "status": {"state": self.builder_state},
                    }
                ]
            )
            if args[0] == "list"
            else ""
        )
        return subprocess.CompletedProcess(args, 0, output, "")

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
    assert len(mounts) == 3
    assert f"source={project.root},target={project.root}" in mounts[0]
    assert "target=/home/agent" in mounts[1]
    assert mounts[2].endswith("target=/etc/agentbox,readonly")
    assert not any(".ssh" in arg or arg == "--ssh" for arg in run)
    config = json.loads((app.store.directory(id) / "control/project.json").read_text())
    assert config["artifacts"] == [".godot", "tests/bin", "tests/obj"]
    assert config["cwd"] == str(project.cwd)
    startup = next(call for call in engine.calls if "new-session" in call)
    assert startup[startup.index("tmux") :] == (
        "tmux",
        "-u",
        "-f",
        "/etc/agentbox/tmux.conf",
        "new-session",
        "-d",
        "-s",
        "agent",
        "-c",
        str(project.cwd),
        "/opt/agentbox/launch.py",
    )


def test_resume_snapshots_and_forks(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    original_id, original = app.store.create()
    (original / "metadata.json").write_text("{}")
    source = original / "home/source.jsonl"
    source.write_text('{"type":"session","id":"original"}\n')
    session = Session(f"{original_id}/original", original_id, "pi", str(project.cwd), source)
    id = app.new(project, harness, no_config=True, session=session)
    home = app.store.directory(id) / "home"
    assert (home / ".agentbox/resume.jsonl").read_text() == source.read_text()
    config = json.loads((app.store.directory(id) / "control/project.json").read_text())
    assert config["command"][-2:] == ["--fork", "/home/agent/.agentbox/resume.jsonl"]
    assert str(source) not in json.dumps(config)


def test_failed_start_removes_vm_but_retains_state(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))

    def fail(id: str) -> None:
        raise AgentboxError("startup failed")

    monkeypatch.setattr(app, "_wait_ready", fail)
    with pytest.raises(AgentboxError, match="startup failed"):
        app.new(project, harness, no_config=True)
    assert engine.calls[-2][0] == "stop"
    assert engine.calls[-1][0] == "delete"
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
    assert engine.calls == [("stop", "ours"), ("delete", "ours")]


def test_accent_is_stable_and_status_cannot_inject_tmux_commands() -> None:
    assert accent("id") == accent("id")
    assert len({accent(str(i)) for i in range(30)}) > 4
    config = tmux_config("id", "#(touch /host/pwned)'", "pi")
    assert "#(touch" not in config
    assert "prefix C-a" in config
    assert "status-style" in config


@pytest.mark.parametrize("path", ["/a,b", "/a\nb"])
def test_mount_rejects_cli_delimiters(path: str) -> None:
    with pytest.raises(AgentboxError):
        mount(Path(path), "/workspace")
