import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from devbox.app import App
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.state import Session, Store
from tests.fakes import FakeEngine
from tests.test_fetch import commit, git


class Guest(FakeEngine):
    """Runs piped exec scripts locally, with the guest checkout path mapped to the clone."""

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

    def exec(self, id: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = super().exec(id, *args, **kwargs)
        if kwargs.get("input") is None:
            return result
        *command, _ = args
        clone = str(self.store.directory(id) / "repo")
        return subprocess.run([*command, clone], input=kwargs["input"], text=True, check=True)


def running(id: str) -> Container:
    return Container(id, "running", "project", "pi", "now")


def test_instance_learns_its_identity_and_constraints(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    assert engine.runs[0].env["DEVBOX_INSTANCE"] == id
    text = (app.store.directory(id) / "home/.devbox/environment.md").read_text()
    assert (
        f"{project.checkout} is a private clone of the user's repository, started from main" in text
    )
    assert f"`devbox review {id}`" in text and f"`devbox sync {id} [branch ...]`" in text


def test_resume_can_continue_from_the_instance_clone(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    first = app.new(project, harness, no_config=True)
    work = commit(app.store.directory(first) / "repo", "work", {"a": "1"})
    source = app.store.directory(first) / "home/source.jsonl"
    source.write_text('{"type":"session","id":"s"}\n')
    session = Session(f"{first}/s", first, "pi", str(project.cwd), source)

    resumed = app.new(project, harness, no_config=True, session=session, from_instance=True)
    clone = app.store.directory(resumed) / "repo"
    assert git(clone, "rev-parse", "HEAD") == work
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "work"
    original = app.store.metadata(first)
    metadata = app.store.metadata(resumed)
    assert (metadata["branch"], metadata["base"]) == (original["branch"], original["base"])
    # Fetch still compares against the original host starting point.
    (branch,) = [b for b in app.fetch(resumed) if b.ref.endswith("/work")]
    assert branch.commits == 1

    fresh = app.new(project, harness, no_config=True, session=session)
    assert git(app.store.directory(fresh) / "repo", "rev-parse", "HEAD") == original["base"]


def test_sync_delivers_host_branches_as_host_refs(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    store = Store(tmp_path / "state")
    app = App(Guest(store), store)
    id = app.new(project, harness, no_config=True)
    clone = app.store.directory(id) / "repo"
    assert app.sync(running(id), []) == ["host/main"]  # nothing new since the clone
    assert git(clone, "rev-parse", "host/main") == app.store.metadata(id)["base"]

    git(project.root, "commit", "--allow-empty", "-qm", "upstream")
    git(project.root, "branch", "other")
    assert app.sync(running(id), ["main", "other"]) == ["host/main", "host/other"]
    assert git(clone, "rev-parse", "host/main") == git(project.root, "rev-parse", "main")
    assert git(clone, "rev-parse", "host/other") == git(project.root, "rev-parse", "other")
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(clone, "rev-parse", "main") == app.store.metadata(id)["base"]


def test_sync_refusals(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="stopped"):
        app.sync(replace(running(id), state="stopped"), [])
    with pytest.raises(DevboxError, match="No branch missing"):
        app.sync(running(id), ["missing"])
    tree = project.root / ".worktrees/loose"
    git(project.root, "worktree", "add", "-q", "--detach", str(tree))
    detached = app.new(Project.discover(tree), harness, no_config=True)
    clone = app.store.directory(detached) / "repo"
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # main is at the same commit
    with pytest.raises(DevboxError, match="name branches"):
        app.sync(running(detached), [])
