import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from devbox.app import App
from devbox.config import ASSETS, Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store, build_files, host_version
from tests.fakes import FakeEngine, FakeRepository


@pytest.mark.parametrize("name", ["pi", "claude"])
def test_built_in_harnesses_share_vim_runtime(name: str, project: Project, tmp_path: Path) -> None:
    harness = replace(Harness.load(ASSETS / "harnesses" / name), version_command=())
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state"), FakeRepository()).build(project, harness, no_config=True)
    _, runtime, prepared = engine.builds
    assert runtime.file == ASSETS / "runtime/Containerfile"
    containerfile = runtime.file.read_text()
    assert "vim" in containerfile.split("apt-get install", 1)[1].split("&&", 1)[0].split()
    assert "ENV EDITOR=vim VISUAL=vim" in containerfile.splitlines()
    assert prepared.args["RUNTIME_IMAGE"] == runtime.tag
    assert "FROM ${RUNTIME_IMAGE}" in (harness.directory / "Containerfile").read_text()


def test_second_new_never_enters_build_scope(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())
    app.new(project, harness, no_config=True)
    app.new(project, harness, no_config=True)
    assert engine.build_scopes == 1
    assert len(engine.builds) == 3
    assert len(engine.image_checks) == 2
    assert engine.image_checks[0] == engine.image_checks[1] == engine.builds[-1].tag
    assert not engine.building_active


@pytest.mark.parametrize("failure", [DevboxError("failed"), KeyboardInterrupt()])
def test_failed_preparation_exits_build_scope_without_creating_instance(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())

    def fail(*args: object, **kwargs: object) -> None:
        assert engine.building_active
        raise failure

    monkeypatch.setattr(engine, "build", fail)
    with pytest.raises(type(failure)):
        app.new(project, harness, no_config=True)
    assert engine.build_scopes == 1
    assert not engine.building_active
    assert not engine.built_images
    assert not app.store.instances.exists()


@pytest.mark.parametrize("packages", ["npm:not-an-array", [42]])
def test_invalid_package_settings_fail_before_build(harness: Harness, packages: object) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text(json.dumps({"packages": packages}))
    with pytest.raises(DevboxError, match="settings.json is invalid"):
        build_files(harness)


def test_explicit_build_rebuilds(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())
    first = app.build(project, harness)
    assert app.build(project, harness) == first
    assert len(engine.builds) == 6
    assert engine.build_scopes == 2
    assert not engine.building_active


def test_package_variant_ignores_private_config_and_filters(project: Project, harness: Harness, tmp_path: Path) -> None:
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
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeRepository())
    image = app._prepared_image(project, harness, files, {})
    settings.write_text(json.dumps({"packages": ["npm:other", "npm:example@1"], "apiKey": "different"}))
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
def test_package_declarations_reject_obvious_secrets_and_invalid_specs(harness: Harness, source: str) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text(json.dumps({"packages": [source]}))
    with pytest.raises(DevboxError):
        build_files(harness)


def test_build_context_never_contains_private_settings(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "settings.json").write_text('{"packages":["npm:example@1"],"apiKey":"secret"}')
    (harness.seed_dir / "auth.json").write_text('"secret"')
    engine = FakeEngine()
    build = engine.build

    def inspect(tag: str, file: Path, context: Path, args: dict[str, str], **kwargs: Any) -> None:
        if "PREPARE_REVISION" in args:
            assert (context / "package-sources.txt").read_text() == "npm:example@1\n"
            assert not any(p.name in ("auth.json", "settings.json") for p in context.rglob("*"))
        build(tag, file, context, args, **kwargs)

    monkeypatch.setattr(engine, "build", inspect)
    App(engine, Store(tmp_path / "state"), FakeRepository()).build(project, harness)


def version_command(output: str, code: int = 0) -> tuple[str, ...]:
    return (sys.executable, "-c", f"print({output!r}); raise SystemExit({code})")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (version_command("2.1.304 (Claude Code)"), "2.1.304"),
        (version_command("1.3.0-beta.2"), "1.3.0-beta.2"),
        (version_command("1.3.0", code=1), None),
        (version_command("dev; rm -rf /"), None),
        (("devbox-missing-harness-cli",), None),
        ((), None),
    ],
)
def test_host_version(harness: Harness, command: tuple[str, ...], expected: str | None) -> None:
    assert host_version(replace(harness, version_command=command)) == expected


def test_host_version_is_a_build_arg_and_image_variant(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())
    first = app.build(project, replace(harness, version_command=version_command("1.3.0")))
    assert engine.builds[-1].args["HARNESS_VERSION"] == "1.3.0"
    upgraded = app.build(project, replace(harness, version_command=version_command("1.4.0")))
    assert upgraded != first
    assert engine.builds[-1].args["HARNESS_VERSION"] == "1.4.0"
    fallback = app.build(project, harness)
    assert "HARNESS_VERSION" not in engine.builds[-1].args
    assert fallback not in (first, upgraded)


def test_architecture_is_an_image_variant(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeRepository())
    monkeypatch.setattr("platform.machine", lambda: "arm64")
    arm = app._prepared_image(project, harness, {}, {})
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert app._prepared_image(project, harness, {}, {}) != arm


def test_unsupported_host_fails_before_building(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    engine = FakeEngine()
    with pytest.raises(DevboxError, match="riscv64"):
        App(engine, Store(tmp_path / "state"), FakeRepository()).new(project, harness, no_config=True)
    assert engine.builds == []
    assert engine.build_scopes == 0
