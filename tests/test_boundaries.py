"""Host reads of guest-writable state must not become a sandbox escape."""

import json
from pathlib import Path

import pytest
from test_app import FakeEngine

from devbox.app import App
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store, snapshot_session


def test_catalog_does_not_follow_home_symlinks(tmp_path: Path) -> None:
    store = Store(tmp_path / "state")
    _, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps({"harness": "pi", "project": "/project", "session_dir": "sessions"})
    )
    outside = tmp_path / "host-secret"
    outside.mkdir()
    (outside / "secret.jsonl").write_text('{"type":"session","id":"private"}\n')
    (directory / "home/sessions").symlink_to(outside, target_is_directory=True)
    assert store.sessions() == []


@pytest.mark.parametrize("symlink_parent", [False, True])
def test_snapshot_rejects_symlink_components(tmp_path: Path, symlink_parent: bool) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.jsonl").write_text('{"id":"private"}\n')
    if symlink_parent:
        (home / "sessions").symlink_to(outside, target_is_directory=True)
    else:
        (home / "sessions").mkdir()
        (home / "sessions/secret.jsonl").symlink_to(outside / "secret.jsonl")
    target = tmp_path / "snapshot"
    with pytest.raises(DevboxError):
        snapshot_session(home / "sessions/secret.jsonl", target, root=home)
    assert not target.exists()


def test_snapshot_refuses_fifo_without_blocking(tmp_path: Path) -> None:
    import os

    source = tmp_path / "fifo"
    os.mkfifo(source)
    with pytest.raises(DevboxError, match="regular file"):
        snapshot_session(source, tmp_path / "target")


def test_state_cannot_be_inside_shared_project(project: Project, harness: Harness) -> None:
    engine = FakeEngine()
    with pytest.raises(DevboxError, match="outside the shared"):
        App(engine, Store(project.root / "state")).new(project, harness)
    assert engine.calls == []
