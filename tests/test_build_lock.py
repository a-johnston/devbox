import fcntl
from dataclasses import replace
from pathlib import Path

import pytest
from test_app import FakeEngine

from agentbox.app import App
from agentbox.config import Harness, Project
from agentbox.errors import AgentboxError
from agentbox.state import Store


def test_build_and_launch_lock_is_exclusive(project: Project, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    with app._project_lock(project):
        path = app.store.root / "build-locks" / app._image_key(project)
        with path.open("w") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_image_names_are_scoped_to_the_same_lock_namespace(
    project: Project, tmp_path: Path
) -> None:
    first = App(FakeEngine(), Store(tmp_path / "one"))
    second = App(FakeEngine(), Store(tmp_path / "two"))
    assert first._image_key(project) != second._image_key(project)


@pytest.mark.parametrize("key", ["PATH", "HOME"])
def test_reserved_environment_rejected_before_build(
    project: Project, harness: Harness, tmp_path: Path, key: str
) -> None:
    engine = FakeEngine()
    with pytest.raises(AgentboxError, match="must not override"):
        App(engine, Store(tmp_path / "state")).new(replace(project, env={key: "/host"}), harness)
    assert engine.calls == []
