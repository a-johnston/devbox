"""Managed instances without a harness, a repository, or a project manifest."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from devbox import app as app_module
from devbox import cli
from devbox.app import App
from devbox.config import ASSETS, Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.repository import Fetched
from devbox.state import Session, Store
from tests.fakes import FakeEngine, FakeGit


@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("allow_root", [False, True])
def test_shell_uses_only_runtime_without_harness_configuration(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: bool, allow_root: bool
) -> None:
    project = replace(project, checkout=project.checkout if workspace else None)
    for name in ("build_files", "host_version", "seed_config", "shared_config", "environment_secrets", "run_hook"):
        monkeypatch.setattr(app_module, name, Mock(side_effect=AssertionError(f"Shell must not call {name}")))
    engine = FakeEngine()
    engine.supports_root = True
    host = FakeGit()
    git = Mock(wraps=host)
    app = App(engine, Store(tmp_path / "state"), git)
    id = app.new(project, None, allow_root=allow_root)
    directory = app.store.directory(id)
    assert len(engine.builds) == 2
    assert engine.builds[-1].labels["dev.devbox.image.kind"] == "runtime"
    assert engine.runs[0].image == engine.builds[-1].tag
    assert engine.runs[0].labels["dev.devbox.harness"] == "shell"
    assert engine.runs[0].allow_root is allow_root
    assert engine.root_enabled == ([id] if allow_root else [])
    assert not engine.execs  # No harness or tmux process is launched.
    metadata = app.store.metadata(id)
    assert metadata["harness"] is metadata["launch"] is metadata["session_dir"] is None
    assert not (directory / "home/.devbox/harness.sh").exists()
    assert not (directory / "home/.pi").exists()
    assert (directory / "repo/.git").is_dir() is workspace
    assert app.store.clones() == ([id] if workspace else [])
    assert app.store.sessions() == []
    if not workspace:
        git.assert_not_called()
        assert (directory / "workspace").is_dir()
        assert engine.runs[0].binds[0] == (str(directory / "workspace"), "/workspace")
    app.attach(Container(id, "running", str(project.root), "shell", "now"))
    assert engine.connections == [(id, ("/bin/bash",), str(project.workdir))]


def test_workspace_free_instance_still_rejects_state_in_image_context(project: Project) -> None:
    project = replace(project, checkout=None)
    engine = FakeEngine()
    app = App(engine, Store(project.context / "state"), Mock())
    with pytest.raises(DevboxError, match="image build context"):
        app.new(project, None)
    assert not engine.builds and not engine.runs


def test_shell_reuses_runtime_image(project: Project, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
    app.new(project, None)
    app.new(project, None)
    assert len(engine.builds) == 2
    assert engine.build_scopes == 1
    assert engine.runs[0].image == engine.runs[1].image


def test_harness_without_checkout_has_scratch_workspace_and_accurate_instructions(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    project = replace(project, checkout=None, config=None)
    engine = FakeEngine()
    git = Mock(side_effect=AssertionError("Must not access host Git without a checkout"))
    app = App(engine, Store(tmp_path / "state"), git)
    id = app.new(project, harness, no_config=True)
    metadata = app.store.metadata(id)
    assert metadata["checkout"] is metadata["base"] is metadata["branch"] is metadata["config"] is None
    assert metadata["workdir"] == metadata["workspace"] == "/workspace"
    directory = app.store.directory(id)
    assert engine.runs[0].binds == (
        (str(directory / "workspace"), "/workspace"),
        (str(directory / "home"), "/home/agent"),
    )
    assert list((directory / "workspace").iterdir()) == []
    assert engine.execs[0].args[3] == "/workspace"
    instructions = (directory / "home/.devbox/environment.md").read_text()
    assert "There is no cloned repository" in instructions
    assert "private clone" not in instructions
    assert "devbox review" not in instructions
    assert f"devbox get {id}" in instructions
    assert not app.store.clones()
    git.assert_not_called()


def test_fork_without_checkout_copies_conversation_but_not_scratch_files(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    project = replace(project, checkout=None)
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), Mock(side_effect=AssertionError("No Git expected")))
    original_id, original = app.store.create()
    (original / "metadata.json").write_text("{}")
    (original / "workspace").mkdir()
    (original / "workspace/private.txt").write_text("not inherited")
    source = original / "home/source.jsonl"
    source.write_text('{"type":"session","id":"original"}\n')
    session = Session(f"{original_id}/original", original_id, harness.name, str(project.cwd), source)
    id = app.new(project, harness, session=session, no_config=True)
    assert list((app.store.directory(id) / "workspace").iterdir()) == []
    assert (app.store.directory(id) / "home/.devbox/resume.jsonl").read_text() == source.read_text()
    assert app.store.metadata(id)["forked_from"] == session.id


@pytest.mark.parametrize("operation", ["fetch", "review", "land", "sync"])
def test_git_commands_reject_workspace_free_instances(tmp_path: Path, operation: str) -> None:
    engine = FakeEngine()
    git = Mock()
    app = App(engine, Store(tmp_path / "state"), git)
    id, directory = app.store.create()
    (directory / "metadata.json").write_text(json.dumps({"checkout": None}))
    container = Container(id, "running", "project", "pi", "now")
    commands = {
        "fetch": lambda: app.fetch(id),
        "review": lambda: app.review(id, "branch"),
        "land": lambda: app.land(id, Fetched("branch", 1, False, (), None, False)),
        "sync": lambda: app.sync(container, ["main"]),
    }
    with pytest.raises(DevboxError, match="has no repository clone"):
        commands[operation]()
    git.assert_not_called()
    assert not engine.execs


def test_landing_does_not_select_workspace_free_instances(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    for checkout in (None, "/host/project"):
        id, directory = app.store.create()
        (directory / "metadata.json").write_text(json.dumps({"root": "/host/project", "checkout": checkout}))
        engine.rows.append(Container(id, "running", "/host/project", "shell", "now"))
    assert app._project_containers("/host/project") == engine.rows[1:]


@pytest.mark.parametrize("preset", [False, True])
def test_fetch_for_stock_or_preset_environment_watches_repository_manifest(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preset: bool
) -> None:
    project = replace(
        project,
        config=tmp_path / "preset/.devbox.toml" if preset else None,
        environment_name="preset" if preset else None,
        containerfile=ASSETS / "default/Containerfile",
        context=ASSETS / "default",
    )
    host = FakeGit()
    app = App(FakeEngine(), Store(tmp_path / "state"), host)
    id, directory = app.store.create()
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "root": str(project.root),
                "config": str(project.config) if project.config else None,
                "environment": project.environment_name,
            }
        )
    )
    resolve = Mock(return_value=project)
    monkeypatch.setattr(app_module.Project, "discover", resolve)
    app.fetch(id)
    resolve.assert_called_once_with(project.root, environment_name=project.environment_name)
    assert host.calls == [
        ("fetch", project.root, directory / "repo", f"refs/devbox/{id}/", [":(literal).devbox.toml"], id.removeprefix("devbox-"))
    ]


def test_environment_presets_have_distinct_image_names(project: Project, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    first = replace(project, config=tmp_path / "first/.devbox.toml", environment_name="first")
    second = replace(project, config=tmp_path / "second/.devbox.toml", environment_name="second")
    assert app._prepared_image(first, None, {}, {}) != app._prepared_image(second, None, {}, {})


@pytest.mark.parametrize("command", ["new", "build"])
def test_shell_cli_resolves_environment_without_loading_a_harness(
    command: str, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    resolve = Mock(return_value=project)
    monkeypatch.setattr(cli.Project, "discover", resolve)
    monkeypatch.setattr(cli, "get_harness", Mock(side_effect=AssertionError("Shell is not a harness")))
    operation = Mock(return_value="devbox-test")
    monkeypatch.setattr(app, command, operation)
    args = [command, "shell", "--environment", "ubuntu", "--project", "/chosen"]
    if command == "new":
        args += ["--detach", "--no-workspace"]
    cli.dispatch(cli.parser().parse_args(args), app)
    resolve.assert_called_once_with(
        Path("/chosen"),
        **({"environment_name": "ubuntu", "workspace": False} if command == "new" else {"environment_name": "ubuntu"}),
    )
    operation.assert_called_once_with(
        project, None, **({"no_config": False, "allow_root": False} if command == "new" else {"no_config": False})
    )


@pytest.mark.parametrize("command", ["fetch", "review", "land"])
def test_git_cli_explains_missing_clone(command: str, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id, directory = app.store.create(["devbox-empty"])
    (directory / "metadata.json").write_text(json.dumps({"checkout": None}))
    with pytest.raises(DevboxError, match=f"{id} has no repository clone"):
        cli.dispatch(cli.parser().parse_args([command, "empty"]), app)


@pytest.mark.parametrize("source", [None, "host"])
def test_fork_cli_preserves_workspace_policy_and_environment(
    source: str | None, project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    session = Session("devbox-empty/c", "devbox-empty", "pi", "/original", tmp_path / "conversation.jsonl")
    monkeypatch.setattr(app.store, "sessions", lambda: [session])
    monkeypatch.setattr(app.store, "clones", lambda: [])
    monkeypatch.setattr(app.store, "metadata", lambda id: {"checkout": None, "environment": "ubuntu"})
    resolve = Mock(return_value=replace(project, checkout=project.checkout if source else None))
    monkeypatch.setattr(cli.Project, "discover", resolve)
    harness = Mock()
    monkeypatch.setattr(cli, "get_harness", lambda name: harness)
    create = Mock(return_value="devbox-new")
    monkeypatch.setattr(app, "new", create)
    cli.dispatch(cli.parser().parse_args(["fork", "empty/c", "--detach", *(["--from", source] if source else [])]), app)
    resolve.assert_called_once_with(Path("/original"), workspace=source == "host", environment_name="ubuntu")
    create.assert_called_once_with(
        resolve.return_value, harness, no_config=False, session=session, from_instance=False, allow_root=False
    )
