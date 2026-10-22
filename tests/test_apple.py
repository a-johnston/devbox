"""Apple-specific CLI and builder behavior, independent of application orchestration."""

import json
import subprocess
from pathlib import Path

import pytest

from devbox.apple import AppleEngine
from devbox.engine import MANAGED
from devbox.errors import DevboxError


@pytest.mark.parametrize("memory", [None, "2G"])
def test_build_translates_arguments_and_host_builder_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, memory: str | None
) -> None:
    if memory:
        monkeypatch.setenv("DEVBOX_BUILDER_MEMORY", memory)
    else:
        monkeypatch.delenv("DEVBOX_BUILDER_MEMORY", raising=False)
    engine = AppleEngine()
    calls = []
    monkeypatch.setattr(engine, "call", lambda *args, **kwargs: calls.append((args, kwargs)))
    engine.build(
        "image:latest",
        tmp_path / "Containerfile",
        tmp_path,
        {"A": "b"},
        platform="linux/arm64",
        cpus=2,
        labels={MANAGED: "1"},
    )
    assert calls == [
        (
            (
                "build",
                "--platform",
                "linux/arm64",
                "--memory",
                memory or "4G",
                "--cpus",
                "2",
                "--tag",
                "image:latest",
                "--file",
                str(tmp_path / "Containerfile"),
                "--build-arg",
                "A=b",
                "--label",
                f"{MANAGED}=1",
                str(tmp_path),
            ),
            {"capture": False},
        )
    ]


@pytest.mark.parametrize("state", ["running", "stopped"])
@pytest.mark.parametrize("failure", [None, DevboxError("failed"), KeyboardInterrupt()])
def test_build_scope_only_stops_a_builder_it_started(
    monkeypatch: pytest.MonkeyPatch, state: str, failure: BaseException | None
) -> None:
    engine = AppleEngine()
    calls = []

    def call(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps([{"status": {"state": state}}]), "")

    monkeypatch.setattr(engine, "call", call)

    def build() -> None:
        with engine.building():
            engine.call("build")
            if failure:
                raise failure

    if failure:
        with pytest.raises(type(failure)):
            build()
    else:
        build()
    assert calls == [
        ("builder", "status", "--format", "json"),
        ("build",),
        *(([("builder", "stop")]) if state == "stopped" else []),
    ]


def test_image_lookup_does_not_misclassify_service_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AppleEngine()
    monkeypatch.setattr(
        engine,
        "call",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "XPC connection invalid"),
    )
    with pytest.raises(DevboxError, match="Cannot check"):
        engine.image_exists("image")


@pytest.mark.parametrize(
    "payload", ["not json", "{}", "null", "[{}]", '[{"id": null}]', '[{"id": ""}]']
)
def test_container_ids_refuse_unrecognizable_inventory(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    engine = AppleEngine()
    monkeypatch.setattr(
        engine, "call", lambda *a, **kw: subprocess.CompletedProcess(a, 0, payload, "")
    )
    with pytest.raises(DevboxError, match="refusing cleanup"):
        engine.container_ids()


def test_container_ids_include_unlabelled_and_stopped_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AppleEngine()
    rows = [{"id": "running"}, {"id": "stopped", "status": {"state": "stopped"}}]
    monkeypatch.setattr(
        engine, "call", lambda *a, **kw: subprocess.CompletedProcess(a, 0, json.dumps(rows), "")
    )
    assert engine.container_ids() == {"running", "stopped"}
