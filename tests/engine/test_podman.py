import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from devbox.engine import MANAGED, RunSpec
from devbox.engine.podman import AGENT_USERNS, PodmanEngine
from devbox.errors import DevboxError


class Recorder:
    """Stands in for PodmanEngine.call, answering by subcommand."""

    def __init__(self, replies: dict[tuple[str, ...], tuple[int, str, str]] | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict] = []
        self.replies = replies or {}

    def __call__(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.kwargs.append(kwargs)
        for prefix, (code, out, err) in self.replies.items():
            if args[: len(prefix)] == prefix:
                return subprocess.CompletedProcess(args, code, out, err)
        return subprocess.CompletedProcess(args, 0, "", "")


def engine_with(
    monkeypatch: pytest.MonkeyPatch, replies: dict | None = None
) -> tuple[PodmanEngine, Recorder]:
    engine = PodmanEngine()
    recorder = Recorder(replies)
    monkeypatch.setattr(engine, "call", recorder)
    return engine, recorder


def test_podman_require_running_reports_info_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, recorder = engine_with(monkeypatch, {("info",): (125, "", "storage is broken")})
    with pytest.raises(DevboxError, match="podman info.*storage is broken"):
        engine.require_running()
    assert recorder.calls == [("info", "--format", "json")]
    assert recorder.kwargs == [{"check": False}]


@pytest.mark.parametrize(
    "info",
    [
        "not json",
        "null",
        "[]",
        "{}",
        '{"host": null}',
        '{"host": {"security": {"rootless": "true"}, "cgroupManager": "systemd", "cgroupVersion": "v2"}}',
        '{"host": {"security": {"rootless": true}, "cgroupManager": "unknown", "cgroupVersion": "v2"}}',
        '{"host": {"security": {"rootless": true}, "cgroupManager": "systemd", "cgroupVersion": null}}',
    ],
)
def test_podman_preflight_rejects_unrecognized_info(
    monkeypatch: pytest.MonkeyPatch, info: str
) -> None:
    engine, _ = engine_with(monkeypatch, {("info",): (0, info, "")})
    with pytest.raises(DevboxError, match="Cannot validate rootless setup"):
        engine.require_running()


@pytest.mark.parametrize(
    "rootless,version,error",
    [(False, "v2", "requires rootless Podman"), (True, "v1", "requires Podman with cgroup v2")],
)
def test_podman_preflight_rejects_unsupported_setup(
    monkeypatch: pytest.MonkeyPatch, rootless: bool, version: str, error: str
) -> None:
    info = {
        "host": {
            "security": {"rootless": rootless},
            "cgroupManager": "systemd",
            "cgroupVersion": version,
        }
    }
    engine, _ = engine_with(monkeypatch, {("info",): (0, json.dumps(info), "")})
    with pytest.raises(DevboxError, match=error):
        engine.require_running()


@pytest.mark.parametrize(
    "runtime,refused",
    [
        ({"name": "krun"}, True),
        ({"name": "crun-krun"}, True),
        ({"name": "crun"}, False),
        (None, False),
    ],
)
def test_podman_refuses_krun_default_runtime(
    monkeypatch: pytest.MonkeyPatch, runtime: dict | None, refused: bool
) -> None:
    host = {"security": {"rootless": True}, "cgroupManager": "systemd", "cgroupVersion": "v2"}
    if runtime is not None:
        host["ociRuntime"] = runtime
    engine, _ = engine_with(monkeypatch, {("info",): (0, json.dumps({"host": host}), "")})
    if refused:
        with pytest.raises(DevboxError, match="krun"):
            engine.require_running()
    else:
        engine.require_running()


def test_podman_missing_executable_names_podman(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*a: object, **kw: object) -> None:
        raise FileNotFoundError("podman")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(DevboxError, match="Podman is required"):
        PodmanEngine().require_running()


@pytest.mark.parametrize("code,exists", [(0, True), (1, False)])
def test_podman_image_exists(monkeypatch: pytest.MonkeyPatch, code: int, exists: bool) -> None:
    engine, recorder = engine_with(monkeypatch, {("image", "exists"): (code, "", "")})
    assert engine.image_exists("img:latest") is exists
    assert recorder.calls == [("image", "exists", "img:latest")]


def test_podman_image_lookup_does_not_misclassify_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = engine_with(monkeypatch, {("image", "exists"): (125, "", "database is locked")})
    with pytest.raises(DevboxError, match="Cannot check prepared image: database is locked"):
        engine.image_exists("img:latest")


def test_podman_building_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, recorder = engine_with(monkeypatch)
    with engine.building():
        pass
    assert recorder.calls == []


def test_podman_build_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, recorder = engine_with(monkeypatch)
    engine.build(
        "tag:latest",
        Path("/ctx/Containerfile"),
        Path("/ctx"),
        {"A": "1", "B": "2"},
        platform="linux/amd64",
        cpus=4,
        labels={MANAGED: "1"},
    )
    assert recorder.calls == [
        (
            "build",
            "--platform",
            "linux/amd64",
            "--tag",
            "tag:latest",
            "--file",
            "/ctx/Containerfile",
            "--build-arg",
            "A=1",
            "--build-arg",
            "B=2",
            "--label",
            f"{MANAGED}=1",
            "/ctx",
        )
    ]
    assert recorder.kwargs == [{"capture": False}]


def test_podman_run_creates_labelled_volumes_then_runs_with_keep_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, recorder = engine_with(monkeypatch)
    engine.run(
        RunSpec(
            name="devbox-1",
            image="img:latest",
            cpus=2,
            memory="4G",
            labels={MANAGED: "1", "dev.devbox.harness": "pi"},
            binds=(("/src", "/src"), ("/state/home", "/home/agent")),
            volumes=(("devbox-1-0", "/src/.venv"), ("devbox-1-1", "/src/dist")),
            env={"A": "b"},
        )
    )
    volume = ("volume", "create", "--label", f"{MANAGED}=1", "--label")
    assert recorder.calls == [
        (*volume, "dev.devbox.instance=devbox-1", "devbox-1-0"),
        (*volume, "dev.devbox.instance=devbox-1", "devbox-1-1"),
        (
            "run",
            "--detach",
            "--init",
            "--name",
            "devbox-1",
            "--userns",
            AGENT_USERNS,
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "all",
            "--cap-add",
            "CHOWN",
            "--cpus",
            "2",
            "--memory",
            "4G",
            "--label",
            f"{MANAGED}=1",
            "--label",
            "dev.devbox.harness=pi",
            "--mount",
            "type=bind,source=/src,target=/src",
            "--mount",
            "type=bind,source=/state/home,target=/home/agent",
            "--env",
            "A=b",
            "--mount",
            "type=volume,source=devbox-1-0,target=/src/.venv",
            "--mount",
            "type=volume,source=devbox-1-1,target=/src/dist",
            "img:latest",
        ),
    ]
    assert AGENT_USERNS == "keep-id:uid=10001,gid=10001"


def row(name: str, state: str, labels: dict[str, str] | None, created: int = 0) -> dict:
    return {"Names": [name], "State": state, "Labels": labels, "Created": created}


def test_podman_containers_filters_by_label_and_maps_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        row("unrelated", "running", None),
        row("other-tool", "running", {"dev.other.managed": "1"}),
        row(
            "devbox-a",
            "running",
            {MANAGED: "1", "dev.devbox.project": "/src", "dev.devbox.harness": "pi"},
            1792609843,
        ),
        row("devbox-b", "exited", {MANAGED: "1"}),
    ]
    engine, recorder = engine_with(monkeypatch, {("ps",): (0, json.dumps(rows), "")})
    containers = engine.containers()
    assert recorder.calls == [("ps", "--all", "--filter", f"label={MANAGED}=1", "--format", "json")]
    assert [(c.id, c.state, c.project, c.harness) for c in containers] == [
        ("devbox-a", "running", "/src", "pi"),
        ("devbox-b", "exited", "?", "?"),
    ]
    assert datetime.fromisoformat(containers[0].created).timestamp() == 1792609843


@pytest.mark.parametrize("payload", ["not json", "{}", '[{"Names": []}]', '[{"State": "x"}]'])
def test_podman_containers_rejects_unexpected_json(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    engine, _ = engine_with(monkeypatch, {("ps",): (0, payload, "")})
    with pytest.raises(DevboxError, match="Unexpected podman JSON"):
        engine.containers()


def test_podman_container_ids_include_unlabelled(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row("unrelated", "running", None), row("devbox-a", "exited", {MANAGED: "1"})]
    engine, recorder = engine_with(monkeypatch, {("ps",): (0, json.dumps(rows), "")})
    assert engine.container_ids() == {"unrelated", "devbox-a"}
    assert recorder.calls == [("ps", "--all", "--format", "json")]


@pytest.mark.parametrize("payload", ["not json", "{}", '[{"Names": []}]', '[{"Names": [""]}]'])
def test_podman_container_ids_refuse_unexpected_json(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    engine, _ = engine_with(monkeypatch, {("ps",): (0, payload, "")})
    with pytest.raises(DevboxError, match="refusing cleanup"):
        engine.container_ids()


def test_podman_exec_passes_secret_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, recorder = engine_with(monkeypatch)
    engine.exec("devbox-a", "tmux", "new-session", env={"TOKEN": "s3cret"})
    assert recorder.calls == [
        ("exec", "--user", "agent", "--env", "TOKEN", "devbox-a", "tmux", "new-session")
    ]
    assert recorder.kwargs[0]["env"] == {"TOKEN": "s3cret"}


def test_podman_connect_strips_outer_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setenv("TMUX", "/host/socket,1,0")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(os, "execvpe", lambda *a: calls.append(a))
    PodmanEngine().connect("devbox-a", "/bin/bash", cwd="/src")
    executable, argv, env = calls[0]
    assert executable == "/usr/bin/podman"
    assert argv == [
        "/usr/bin/podman",
        "exec",
        "-it",
        "--user",
        "agent",
        "--workdir",
        "/src",
        "devbox-a",
        "/bin/bash",
    ]
    assert "TMUX" not in env


def test_podman_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, recorder = engine_with(monkeypatch)
    engine.stop("devbox-a")
    assert recorder.calls == [("stop", "devbox-a")]


@pytest.mark.parametrize("listed", ["devbox-a-0\ndevbox-a-1\n", ""])
def test_podman_remove_deletes_only_labelled_volumes(
    monkeypatch: pytest.MonkeyPatch, listed: str
) -> None:
    engine, recorder = engine_with(monkeypatch, {("volume", "ls"): (0, listed, "")})
    engine.remove("devbox-a")
    assert recorder.calls == [
        ("stop", "devbox-a"),
        ("rm", "--ignore", "devbox-a"),
        (
            "volume",
            "ls",
            "--filter",
            "label=dev.devbox.instance=devbox-a",
            "--format",
            "{{.Name}}",
        ),
        *(("volume", "rm", name) for name in listed.split()),
    ]
    assert recorder.kwargs[0] == {"check": False}


def test_podman_run_explains_unreachable_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    error = "crun: sd-bus call: Process org.freedesktop.systemd1 exited with status 1"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 126, "", error)
    )
    spec = RunSpec("devbox-1", "img", 1, "1G", {}, (), (), {})
    with pytest.raises(
        DevboxError, match=r"(?s)sd-bus call.*DBUS_SESSION_BUS_ADDRESS.*\[engine\] cgroup_manager"
    ):
        PodmanEngine().run(spec)


@pytest.mark.parametrize(
    "manager,rootless,warned", [("cgroupfs", True, True), ("systemd", True, False)]
)
def test_podman_warns_when_rootless_cgroupfs_ignores_limits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manager: str,
    rootless: bool,
    warned: bool,
) -> None:
    info = {
        "host": {
            "cgroupManager": manager,
            "cgroupVersion": "v2",
            "security": {"rootless": rootless},
        }
    }
    engine, _ = engine_with(monkeypatch, {("info",): (0, json.dumps(info), "")})
    engine.require_running()
    engine.run(RunSpec("devbox-1", "img", 1, "1G", {}, (), (), {}))
    assert ("not enforced" in capsys.readouterr().err) is warned
