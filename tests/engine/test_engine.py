import json
import os
import subprocess

import pytest

from devbox.config import ASSETS
from devbox.engine import MANAGED, RunSpec, host_platform, select_engine
from devbox.engine.apple import AppleEngine, builder_memory
from devbox.engine.engine import TERMINAL
from devbox.engine.podman import PodmanEngine
from devbox.errors import DevboxError


def test_ownership_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    def row(id: str, labels: dict[str, str]) -> dict:
        return {"id": id, "status": {"state": "running"}, "configuration": {"labels": labels}}

    data = [
        row("unrelated", {}),
        row("other-tool", {"dev.other.managed": "1"}),
        row("ours", {MANAGED: "1"}),
    ]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(data), ""))
    assert [c.id for c in AppleEngine().containers()] == ["ours"]


def test_apple_remove_stops_then_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    engine = AppleEngine()
    monkeypatch.setattr(engine, "call", lambda *a, **kw: calls.append(a))
    engine.remove("ours")
    assert calls == [("stop", "ours"), ("delete", "ours")]


@pytest.mark.parametrize("engine", [AppleEngine(), PodmanEngine()])
def test_exec_input_is_piped_interactively(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runs.append((argv, kwargs["input"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    engine.exec("box", "cat", input="data")
    engine.exec("box", "true")
    assert runs == [
        ([engine.executable, "exec", "--user", "agent", "--interactive", "box", "cat"], "data"),
        ([engine.executable, "exec", "--user", "agent", "box", "true"], None),
    ]


@pytest.mark.parametrize("backend", [AppleEngine, PodmanEngine])
def test_enables_root_using_only_the_trusted_setup_script(monkeypatch: pytest.MonkeyPatch, backend: type) -> None:
    calls = []
    engine = backend()
    assert engine.supports_root
    monkeypatch.setattr(engine, "call", lambda *a, **kw: calls.append((a, kw)))
    engine.enable_root("box")
    assert calls == [
        (
            ("exec", "--user", "root", "--interactive", "box", "sh", "-s", "--", "/etc/sudoers.d"),
            {"input": (ASSETS / "runtime/allow-root.sh").read_text()},
        ),
        (("exec", "--user", "agent", "box", "sudo", "-n", "--", "true"), {"check": True, "env": None, "input": None}),
    ]


@pytest.mark.parametrize("backend", [AppleEngine, PodmanEngine])
@pytest.mark.parametrize("failed_call", [1, 2])
def test_root_setup_and_sudo_probe_fail_closed(monkeypatch: pytest.MonkeyPatch, backend: type, failed_call: int) -> None:
    calls = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1 if len(calls) == failed_call else 0, "", "sudo unavailable")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DevboxError, match="sudo unavailable"):
        backend().enable_root("box")
    assert len(calls) == failed_call


@pytest.mark.parametrize("operation", ["run", "enable_root"])
def test_unsupported_backend_refuses_guest_root_without_calling_the_runtime(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    class UnsupportedEngine(AppleEngine):
        name = "unsupported"
        supports_root = False

    engine = UnsupportedEngine()
    calls = []
    monkeypatch.setattr(engine, "call", lambda *a, **kw: calls.append(a))
    with pytest.raises(DevboxError, match="--allow-root is not supported by the unsupported backend"):
        if operation == "run":
            engine.run(RunSpec("box", "img", 1, "1G", {}, (), {}, allow_root=True))
        else:
            engine.enable_root("box")
    assert not calls


def test_stopped_service_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "apiserver is not running", ""),
    )
    with pytest.raises(DevboxError, match="container system start"):
        AppleEngine().require_running()


@pytest.mark.parametrize("term", ["xterm-ghostty", None])
def test_connect_replaces_process_and_strips_outer_tmux(monkeypatch: pytest.MonkeyPatch, term: str | None) -> None:
    calls = []
    monkeypatch.setenv("TMUX", "/host/socket,1,0")
    if term:
        monkeypatch.setenv("TERM", term)
    else:
        monkeypatch.delenv("TERM", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: "/bin/container")
    monkeypatch.setattr(os, "execvpe", lambda *a: calls.append(a))
    AppleEngine().connect("ours", "tmux", "attach-session", "-t", "agent")
    executable, argv, env = calls[0]
    assert executable == "/bin/container"
    assert argv == [
        "/bin/container",
        "exec",
        "-it",
        "--user",
        "agent",
        *(["--env", "TERM"] if term else []),
        "ours",
        "sh",
        "-c",
        TERMINAL,
        "sh",
        "tmux",
        "attach-session",
        "-t",
        "agent",
    ]
    assert "TMUX" not in env
    assert env.get("TERM") == term


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        ("xterm-256color", "xterm-256color"),
        ("no-such-terminal", "xterm-256color"),
        ("", "xterm-256color"),
    ],
)
def test_terminal_falls_back_without_terminfo(term: str, expected: str) -> None:
    if subprocess.run(["infocmp", "xterm-256color"], capture_output=True).returncode:
        pytest.skip("no xterm-256color terminfo on this host")
    result = subprocess.run(
        ["sh", "-c", TERMINAL, "sh", "sh", "-c", 'printf %s "$TERM"'],
        env={**os.environ, "TERM": term},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == expected


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


def test_builder_memory_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVBOX_BUILDER_MEMORY", "lots")
    with pytest.raises(DevboxError, match="DEVBOX_BUILDER_MEMORY"):
        builder_memory()


@pytest.mark.parametrize("allow_root", [False, True])
def test_apple_run_translates_spec(monkeypatch: pytest.MonkeyPatch, allow_root: bool) -> None:
    calls = []
    engine = AppleEngine()
    monkeypatch.setattr(engine, "call", lambda *a, **kw: calls.append(a))
    engine.run(
        RunSpec(
            name="devbox-1",
            image="img:latest",
            cpus=2,
            memory="4G",
            labels={MANAGED: "1"},
            binds=(("/src", "/src"),),
            env={"A": "b"},
            allow_root=allow_root,
        )
    )
    assert calls == [
        (
            "run",
            "--detach",
            "--init",
            "--name",
            "devbox-1",
            "--cpus",
            "2",
            "--memory",
            "4G",
            "--label",
            f"{MANAGED}=1",
            "--mount",
            "type=bind,source=/src,target=/src",
            "--env",
            "A=b",
            "img:latest",
        )
    ]


@pytest.mark.parametrize(
    "platform,expected",
    [("darwin", AppleEngine), ("linux", PodmanEngine)],
)
def test_select_engine(monkeypatch: pytest.MonkeyPatch, platform: str, expected: type) -> None:
    monkeypatch.setattr("sys.platform", platform)
    # The old override must no longer change backend selection.
    monkeypatch.setenv("DEVBOX_ENGINE", "unsupported")
    assert type(select_engine()) is expected


def test_select_engine_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(DevboxError, match="win32"):
        select_engine()
