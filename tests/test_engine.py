import json
import os
import subprocess

import pytest

from devbox.engine import MANAGED, Engine, host_platform
from devbox.errors import DevboxError


def test_ownership_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    def row(id: str, labels: dict[str, str]) -> dict:
        return {"id": id, "status": {"state": "running"}, "configuration": {"labels": labels}}

    data = [
        row("unrelated", {}),
        row("other-tool", {"dev.other.managed": "1"}),
        row("ours", {MANAGED: "1"}),
    ]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(data), "")
    )
    assert [c.id for c in Engine().containers()] == ["ours"]


def test_remove_deletes_only_this_containers_volumes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    volumes = [{"id": "ours-0"}, {"id": "ours-1"}, {"id": "ours2-0"}, {"id": "other"}]

    def call(*args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(volumes), "")

    engine = Engine()
    monkeypatch.setattr(engine, "call", call)
    engine.remove("ours")
    assert calls == [
        ("stop", "ours"),
        ("delete", "ours"),
        ("volume", "list", "--format", "json"),
        ("volume", "delete", "ours-0"),
        ("volume", "delete", "ours-1"),
    ]


def test_stopped_service_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "apiserver is not running", ""),
    )
    with pytest.raises(DevboxError, match="container system start"):
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
    with pytest.raises(DevboxError, match="Unsupported host architecture"):
        host_platform()
