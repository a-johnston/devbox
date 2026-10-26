import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.state import Session, Store, shared_config
from tests.fakes import FakeEngine


def test_shared_auth_is_empty_private_and_not_imported_from_host(harness: Harness, tmp_path: Path) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "auth.json").write_text('"host refresh token"')
    paths = shared_config(harness)
    assert paths == {"auth": tmp_path / "config/devbox/shared/pi/auth"}
    assert list(paths["auth"].iterdir()) == []
    for path in (paths["auth"], paths["auth"].parent, paths["auth"].parent.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert "auth.json" not in harness.seed_files


def test_instances_share_only_the_dedicated_auth_directory(project: Project, harness: Harness, tmp_path: Path) -> None:
    harness.seed_dir.mkdir()
    (harness.seed_dir / "auth.json").write_text('"host refresh token"')
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    first = app.new(project, harness)
    shared = shared_config(harness)["auth"]
    (shared / "auth.json").write_text('"guest refresh token"')
    second = app.new(project, harness)
    for run in engine.runs:
        assert len(run.binds) == 3
        assert run.binds[-1] == (str(shared), "/home/agent/.devbox/pi-auth")
        assert run.env["DEVBOX_SHARED_AUTH"] == "/home/agent/.devbox/pi-auth"
        assert str(harness.seed_dir) not in str(run.binds)
    for id in (first, second):
        home = app.store.directory(id) / "home"
        assert not (home / harness.state_dir / "auth.json").exists()
    assert (shared / "auth.json").read_text() == '"guest refresh token"'
    # State cleanup and container deletion must not revoke shared authentication.
    engine.remove(first)
    app.store.remove_instance(first)
    assert (shared / "auth.json").read_text() == '"guest refresh token"'
    assert "host refresh token" in (harness.seed_dir / "auth.json").read_text()


def test_resumed_instances_keep_the_shared_auth_mount(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    original = app.new(project, harness)
    source = app.store.directory(original) / "home/source.jsonl"
    source.write_text('{"type":"session","id":"saved"}\n')
    session = Session(f"{original}/saved", original, "pi", str(project.cwd), source)
    resumed = app.new(project, harness, session=session)
    assert engine.runs[0].binds[-1] == engine.runs[1].binds[-1]
    assert engine.runs[1].env["DEVBOX_SHARED_AUTH"] == "/home/agent/.devbox/pi-auth"
    assert engine.execs[-1].args[-2:] == ("--fork", "/home/agent/.devbox/resume.jsonl")
    assert (app.store.directory(resumed) / "home/.devbox/resume.jsonl").read_text() == source.read_text()


def test_no_config_has_no_shared_mount_environment_or_host_auth(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(replace(project, env={"DEVBOX_SHARED_AUTH": "/unexpected"}), harness, no_config=True)
    assert len(engine.runs[0].binds) == 2
    assert "DEVBOX_SHARED_AUTH" not in engine.runs[0].env
    assert not (tmp_path / "config/devbox/shared").exists()
    assert not (app.store.directory(id) / "home" / harness.state_dir / "auth.json").exists()


def test_build_does_not_prepare_or_mount_shared_credentials(project: Project, harness: Harness, tmp_path: Path) -> None:
    App(FakeEngine(), Store(tmp_path / "state")).build(project, harness)
    assert not (tmp_path / "config/devbox/shared").exists()


def test_profiles_without_shared_dirs_do_not_create_them(harness: Harness, tmp_path: Path) -> None:
    assert shared_config(replace(harness, shared_dirs={})) == {}
    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize("component", ["shared", "pi", "auth"])
def test_shared_directory_symlinks_are_rejected(harness: Harness, tmp_path: Path, component: str) -> None:
    path = tmp_path / "config/devbox/shared"
    if component != "shared":
        path /= "pi"
    if component == "auth":
        path /= "auth"
    path.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DevboxError, match="symlinked shared"):
        shared_config(harness)
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode
    assert list(outside.iterdir()) == []


def test_existing_file_at_shared_directory_is_reported(harness: Harness, tmp_path: Path) -> None:
    root = tmp_path / "config/devbox/shared/pi"
    root.mkdir(parents=True)
    (root / "auth").write_text("not a directory")
    with pytest.raises(DevboxError, match="Cannot prepare shared state"):
        shared_config(harness)


def test_shared_auth_contents_are_not_followed_or_modified(harness: Harness, tmp_path: Path) -> None:
    path = shared_config(harness)["auth"]
    outside = tmp_path / "host-file"
    outside.write_text("host data")
    (path / "auth.json").symlink_to(outside)
    (path / "auth.json.lock").mkdir()
    assert shared_config(harness)["auth"] == path
    assert outside.read_text() == "host data"
    assert (path / "auth.json.lock").is_dir()


def test_shared_auth_defaults_to_user_config_directory(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert shared_config(harness)["auth"] == tmp_path / "home/.config/devbox/shared/pi/auth"


def test_shared_state_inside_project_is_rejected(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project.root / "config"))
    engine = FakeEngine()
    with pytest.raises(DevboxError, match="shared harness state outside"):
        App(engine, Store(tmp_path / "state")).new(project, harness)
    assert not engine.runs
    assert not (project.root / "config").exists()


def test_shared_state_inside_symlinked_project_is_rejected(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    link = tmp_path / "link"
    link.symlink_to(project)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project / "config"))
    with pytest.raises(DevboxError, match="shared harness state outside"):
        shared_config(harness, project_root=link)
    assert not (project / "config").exists()


def test_shared_config_resolves_a_relative_config_base(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    assert shared_config(harness)["auth"] == tmp_path / "relative-config/devbox/shared/pi/auth"


@pytest.mark.parametrize(
    "entry",
    [
        'auth = "../outside"',
        'auth = "/host"',
        'auth = ".pi/agent"',
        'auth = ".pi/agent/auth"',
        'auth = ".pi"',
        'auth = ".devbox"',
        'auth = ".devbox/environment.md"',
        'auth = ".devbox/harness.sh"',
        'auth = ".devbox/resume.jsonl"',
        '"../auth" = "shared"',
        'AUTH = "shared"',
        "auth = 3",
        'auth = "shared"\nother = "shared/nested"',
    ],
)
def test_shared_directory_profile_validation(harness: Harness, tmp_path: Path, entry: str) -> None:
    profile = (harness.directory / "profile.toml").read_text().split("[shared_dirs]")[0]
    (tmp_path / "profile.toml").write_text(profile + "[shared_dirs]\n" + entry + "\n")
    (tmp_path / "Containerfile").touch()
    with pytest.raises(DevboxError):
        Harness.load(tmp_path)


def test_shared_config_can_use_multiple_named_directories(harness: Harness, tmp_path: Path) -> None:
    paths = shared_config(replace(harness, shared_dirs={"auth": "shared/auth", "cache": "cache"}))
    assert set(paths) == {"auth", "cache"}
    assert all(path.is_dir() for path in paths.values())
    assert all(path.parent == tmp_path / "config/devbox/shared/pi" for path in paths.values())
    assert os.listdir(paths["auth"]) == []
