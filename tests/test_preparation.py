import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_app import FakeEngine

from agentbox.app import App
from agentbox.config import Harness, Project
from agentbox.engine import Engine
from agentbox.errors import AgentboxError
from agentbox.state import Store, build_files, host_version


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

    def fail_build(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
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
        build_files(harness)


def test_explicit_build_rebuilds_with_builder_memory(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    first = app.build(replace(project, memory="8G", builder_memory="2G"), harness)
    assert app.build(project, harness) == first
    builds = [c for c in engine.calls if c[0] == "build"]
    assert [c[c.index("--memory") + 1] for c in builds] == ["2G"] * 3 + ["4G"] * 3


@pytest.mark.parametrize("state,stops", [("running", 0), ("stopped", 1)])
def test_only_a_builder_we_started_is_stopped(
    project: Project, harness: Harness, tmp_path: Path, state: str, stops: int
) -> None:
    engine = FakeEngine()
    engine.builder_state = state
    App(engine, Store(tmp_path / "state")).build(project, harness)
    assert sum(c[:2] == ("builder", "stop") for c in engine.calls) == stops


@pytest.mark.parametrize("failure", [AgentboxError("failed"), KeyboardInterrupt()])
def test_builder_stopped_on_failure_or_cancellation(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    engine = FakeEngine()

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(engine, "build", fail)
    with pytest.raises(type(failure)):
        App(engine, Store(tmp_path / "state")).build(project, harness)
    assert engine.builder_state == "stopped"
    assert engine.calls[-1] == ("builder", "stop")


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
    files = build_files(harness)
    assert files == {"package-sources.txt": "npm:example@1\nnpm:other\n"}
    assert build_files(harness, no_config=True) == {}
    app = App(FakeEngine(), Store(tmp_path / "state"))
    image = app._prepared_image(project, harness, files, {})
    settings.write_text(
        json.dumps({"packages": ["npm:other", "npm:example@1"], "apiKey": "different"})
    )
    assert app._prepared_image(project, harness, build_files(harness), {}) == image
    assert app._prepared_image(project, harness, {}, {}) != image


@pytest.mark.parametrize(
    "source",
    [
        "https://token@github.com/example/package",
        "git:https://github.com/example/package?token=private",
        "npm:pkg\nsecret",
    ],
)
def test_package_declarations_reject_obvious_secrets_and_invalid_specs(
    harness: Harness, source: str
) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text(json.dumps({"packages": [source]}))
    with pytest.raises(AgentboxError):
        build_files(harness)


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

    def inspect(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "build" and "PREPARE_REVISION=" in " ".join(args):
            context = Path(args[-1])
            assert (context / "package-sources.txt").read_text() == "npm:example@1\n"
            assert not any(p.name in ("auth.json", "settings.json") for p in context.rglob("*"))
        return call(*args, **kwargs)

    monkeypatch.setattr(engine, "call", inspect)
    App(engine, Store(tmp_path / "state")).build(project, harness)


def version_command(output: str, code: int = 0) -> tuple[str, ...]:
    return (sys.executable, "-c", f"print({output!r}); raise SystemExit({code})")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (version_command("2.1.304 (Claude Code)"), "2.1.304"),
        (version_command("1.3.0-beta.2"), "1.3.0-beta.2"),
        (version_command("1.3.0", code=1), None),
        (version_command("dev; rm -rf /"), None),
        (("agentbox-missing-harness-cli",), None),
        ((), None),
    ],
)
def test_host_version(harness: Harness, command: tuple[str, ...], expected: str | None) -> None:
    assert host_version(replace(harness, version_command=command)) == expected


def test_host_version_is_a_build_arg_and_image_variant(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    first = app.build(project, replace(harness, version_command=version_command("1.3.0")))
    assert "HARNESS_VERSION=1.3.0" in engine.calls[-2]
    upgraded = app.build(project, replace(harness, version_command=version_command("1.4.0")))
    assert upgraded != first
    fallback = app.build(project, harness)
    assert not any("HARNESS_VERSION" in arg for arg in engine.calls[-2])
    assert fallback not in (first, upgraded)
