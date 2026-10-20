import json
import os
import subprocess

import pytest

from agentbox.engine import MANAGED, OWNER, Container, Engine
from agentbox.errors import AgentboxError


def test_ownership_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    def row(id: str, labels: dict[str, str]) -> dict:
        return {"id": id, "status": {"state": "running"}, "configuration": {"labels": labels}}

    data = [
        row("unrelated", {}),
        row("wrong-owner", {MANAGED: "1", OWNER: "999999"}),
        row("ours", {MANAGED: "1", OWNER: str(os.getuid())}),
    ]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(data), "")
    )
    assert [c.id for c in Engine().containers()] == ["ours"]


def test_remove_never_uses_global_all(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    engine = Engine()
    monkeypatch.setattr(engine, "call", lambda *a, **kw: calls.append(a))
    engine.remove(Container("ours", "running", "p", "pi", "now"))
    assert calls == [("stop", "ours"), ("delete", "ours")]


def test_stopped_service_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "apiserver is not running", ""),
    )
    with pytest.raises(AgentboxError, match="container system start"):
        Engine().require_running()


def test_connect_replaces_process_and_strips_outer_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setenv("TMUX", "/host/socket,1,0")
    monkeypatch.setattr("shutil.which", lambda name: "/bin/container")
    monkeypatch.setattr(os, "execvpe", lambda *a: calls.append(a))
    Engine().connect("ours", "tmux", "attach-session", "-t", "agent")
    executable, argv, env = calls[0]
    assert executable == "/bin/container"
    assert argv == [
        "/bin/container",
        "exec",
        "-it",
        "--user",
        "agent",
        "ours",
        "tmux",
        "attach-session",
        "-t",
        "agent",
    ]
    assert "TMUX" not in env
