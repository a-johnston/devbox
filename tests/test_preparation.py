import fcntl
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from test_app import FakeEngine

from agentbox.app import App
from agentbox.config import Harness, Project
from agentbox.engine import Engine
from agentbox.errors import AgentboxError
from agentbox.state import Store, package_sources


def test_second_new_never_starts_or_checks_builder(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    app.new(project, harness, no_config=True)
    assert engine.builder_state == "stopped"
    engine.calls.clear()
    app.new(project, harness, no_config=True)
    assert not any(c[0] in ("build", "builder", "list") for c in engine.calls)
    assert engine.calls[0][:2] == ("image", "inspect")


def test_failed_preparation_does_not_publish_or_create_instance(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    call = engine.call

    def fail_build(*args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        if args[0] == "build":
            raise AgentboxError("intentional build failure")
        return call(*args, **kwargs)

    monkeypatch.setattr(engine, "call", fail_build)
    with pytest.raises(AgentboxError, match="intentional"):
        app.new(project, harness, no_config=True)
    assert engine.builder_state == "stopped"
    assert not engine.images
    assert not app.store.instances.exists()


@pytest.mark.parametrize("packages", ["npm:not-an-array", [42]])
def test_invalid_package_settings_fail_before_build(harness: Harness, packages: object) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text(json.dumps({"packages": packages}))
    with pytest.raises(AgentboxError, match="settings.json is invalid"):
        package_sources(harness)


def test_explicit_build_rebuilds_and_uses_separate_memory(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    first = app.build(replace(project, memory="8G", builder_memory="2G"), harness)
    assert app.build(project, harness) == first
    builds = [c for c in engine.calls if c[0] == "build"]
    assert len(builds) == 6
    assert all(c[c.index("--memory") + 1] == "2G" for c in builds[:3])
    assert all(c[c.index("--memory") + 1] == "4G" for c in builds[3:])


@pytest.mark.parametrize(
    "state,keep,stops", [("running", False, 0), ("stopped", False, 1), ("stopped", True, 0)]
)
def test_builder_lifetime(state: str, keep: bool, stops: int) -> None:
    engine = FakeEngine()
    engine.builder_state = state
    with engine.builder(memory="2G", cpus=2, keep=keep):
        assert engine.builder_state == "running"
    assert sum(c[:2] == ("builder", "stop") for c in engine.calls) == stops
    if state == "running":
        assert not any(c[0] == "builder" for c in engine.calls)


@pytest.mark.parametrize("failure", [AgentboxError("failed"), KeyboardInterrupt()])
def test_builder_cleanup_on_failure_or_cancellation(failure: BaseException) -> None:
    engine = FakeEngine()
    with pytest.raises(type(failure)), engine.builder(memory="2G", cpus=2):
        raise failure
    assert engine.calls[-1] == ("builder", "stop")


def test_builder_cleanup_on_start_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    call = engine.call

    def interrupt(*args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        result = call(*args, **kwargs)
        if args[:2] == ("builder", "start"):
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(engine, "call", interrupt)
    with pytest.raises(KeyboardInterrupt), engine.builder(memory="2G", cpus=2):
        pytest.fail("should not reach the build")
    assert engine.builder_state == "stopped"


def test_builder_cleanup_does_not_hide_original_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = FakeEngine()
    call = engine.call

    def fail_stop(*args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("builder", "stop"):
            raise AgentboxError("stop failed")
        return call(*args, **kwargs)

    monkeypatch.setattr(engine, "call", fail_stop)
    with pytest.raises(AgentboxError, match="original"), engine.builder(memory="2G", cpus=2):
        raise AgentboxError("original")
    assert "cleanup failed" in capsys.readouterr().err


def test_builder_lock_shared_across_projects_and_stores(tmp_path: Path) -> None:
    from agentbox.engine import BUILDER_LOCK

    first = App(FakeEngine(), Store(tmp_path / "one"))
    second = App(FakeEngine(), Store(tmp_path / "two"))
    with first.engine.builder(memory="2G", cpus=2):
        with BUILDER_LOCK.open("a") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert second.engine.executable == first.engine.executable


@pytest.mark.parametrize(
    "payload",
    [
        "garbage",
        "null",
        '[{"id":"buildkit","configuration":{"labels":{}},"status":{"state":"running"}}]',
    ],
)
def test_unknown_builder_state_never_stops_anything(
    payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(
        engine, "call", lambda *a, **kw: subprocess.CompletedProcess(a, 0, payload, "")
    )
    with pytest.raises(AgentboxError), engine.builder(memory="2G", cpus=2):
        pytest.fail("must not claim ownership")


def test_image_lookup_does_not_misclassify_service_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine()
    monkeypatch.setattr(
        engine,
        "call",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "XPC connection invalid"),
    )
    with pytest.raises(AgentboxError, match="Cannot check"):
        engine.image_exists("image")


def test_package_variant_ignores_private_config_and_filters(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    harness.seed_dir.mkdir()
    settings = harness.seed_dir / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "packages": ["npm:example@1", {"source": "npm:other", "skills": []}, "/host/local"],
                "apiKey": "secret",
            }
        )
    )
    sources = package_sources(harness)
    assert sources == ("npm:example@1", "npm:other")
    assert package_sources(harness, no_config=True) == ()
    app = App(FakeEngine(), Store(tmp_path / "state"))
    image = app._prepared_image(project, harness, sources)
    settings.write_text(
        json.dumps({"packages": ["npm:other", "npm:example@1"], "apiKey": "different"})
    )
    assert app._prepared_image(project, harness, package_sources(harness)) == image
    assert app._prepared_image(project, harness, ()) != image


@pytest.mark.parametrize(
    "source",
    [
        "https://token@github.com/example/package",
        "git:https://github.com/example/package?token=private",
        "npm:-malicious",
        "npm:pkg\nsecret",
    ],
)
def test_package_declarations_reject_obvious_secrets_and_invalid_specs(
    harness: Harness, source: str
) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text(json.dumps({"packages": [source]}))
    with pytest.raises(AgentboxError):
        package_sources(harness)


def test_build_context_never_contains_private_settings(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text(
        '{"packages":["npm:example@1"],"apiKey":"secret"}'
    )
    (harness.seed_dir / "auth.json").write_text('"secret"')
    engine = FakeEngine()
    call = engine.call

    def inspect(*args: str, **kwargs: bool) -> subprocess.CompletedProcess[str]:
        if args[0] == "build" and "PREPARE_REVISION=" in " ".join(args):
            context = Path(args[-1])
            assert json.loads((context / "package-sources.json").read_text()) == ["npm:example@1"]
            assert not any(p.name in ("auth.json", "settings.json") for p in context.rglob("*"))
        return call(*args, **kwargs)

    monkeypatch.setattr(engine, "call", inspect)
    App(engine, Store(tmp_path / "state")).build(project, harness)
