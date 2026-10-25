import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from devbox.app import App
from devbox.cli import dispatch, parser
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
        checkout = self.store.metadata(id)["checkout"]
        clone = str(self.store.directory(id) / "repo")
        command = [clone if arg == checkout else arg for arg in args]
        return subprocess.run(
            command, input=kwargs["input"], text=True, check=True, capture_output=True
        )


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
    # Fetch counts the resumed work against the current host HEAD.
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
    assert app.sync(running(id), []) == ["main: up to date"]  # nothing new since the clone
    assert git(clone, "rev-parse", "host/main") == app.store.metadata(id)["base"]

    git(project.root, "commit", "--allow-empty", "-qm", "upstream")
    git(project.root, "branch", "other")
    assert app.sync(running(id), ["main", "other"]) == [
        "main: fast-forwarded",
        "other: delivered as host/other (no local branch)",
    ]
    assert git(clone, "rev-parse", "host/main") == git(project.root, "rev-parse", "main")
    assert git(clone, "rev-parse", "host/other") == git(project.root, "rev-parse", "other")
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(clone, "rev-parse", "main") == git(project.root, "rev-parse", "main")


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


@pytest.fixture
def guest(project: Project, harness: Harness, tmp_path: Path) -> tuple[App, str, Path]:
    git(project.root, "config", "user.name", "Test")
    git(project.root, "config", "user.email", "test@example.invalid")
    store = Store(tmp_path / "state")
    app = App(Guest(store), store)
    id = app.new(project, harness, no_config=True)
    clone = store.directory(id) / "repo"
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.invalid")
    return app, id, clone


def advance(project: Project) -> str:
    git(project.root, "commit", "--allow-empty", "-qm", "upstream")
    return git(project.root, "rev-parse", "main")


def test_sync_fast_forwards_an_idle_local_branch(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    git(clone, "switch", "-qc", "feature")
    tip = advance(project)
    assert app.sync(running(id), ["main"]) == ["main: fast-forwarded"]
    assert git(clone, "rev-parse", "main") == tip
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "feature"


def test_sync_fast_forwards_the_clean_checked_out_branch(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    (clone / "untracked").write_text("kept")
    tip = advance(project)
    assert app.sync(running(id), ["main"]) == ["main: fast-forwarded"]
    assert git(clone, "rev-parse", "HEAD") == tip
    assert (clone / "untracked").read_text() == "kept"


def test_sync_leaves_a_checked_out_branch_with_uncommitted_changes(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    base = git(clone, "rev-parse", "HEAD")
    name = git(clone, "ls-files").splitlines()[0]
    (clone / name).write_text("edited")
    tip = advance(project)
    (line,) = app.sync(running(id), ["main"])
    assert line.startswith("main: left alone (uncommitted changes)")
    assert git(clone, "rev-parse", "HEAD") == base
    assert git(clone, "rev-parse", "host/main") == tip
    assert (clone / name).read_text() == "edited"


def test_sync_leaves_a_branch_during_a_git_operation(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    base = git(clone, "rev-parse", "HEAD")
    (clone / ".git/rebase-merge").mkdir()
    advance(project)
    (line,) = app.sync(running(id), ["main"])
    assert "a Git operation is in progress" in line
    assert git(clone, "rev-parse", "HEAD") == base


def test_sync_leaves_diverged_and_ahead_branches(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    ahead = commit(clone, "ahead", {"a": "1"})
    side = git(project.root, "commit-tree", "-m", "host side", "-p", "main", "main^{tree}")
    git(project.root, "update-ref", "refs/heads/ahead", side)
    assert app.sync(running(id), ["ahead"]) == ["ahead: diverged from host/ahead; left alone"]
    git(project.root, "update-ref", "refs/heads/ahead", "main")
    assert app.sync(running(id), ["ahead"]) == ["ahead: has commits beyond host/ahead; left alone"]
    assert git(clone, "rev-parse", "ahead") == ahead


def test_sync_rewrites_a_branch_only_while_it_is_unchanged(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    old = commit(clone, "feature", {"a": "1"})
    new = advance(project)  # Stands in for the rebased commits, which main contains.
    assert app.sync(running(id), ["main"], [("feature", old, new)]) == [
        "feature: moved to the rebased commits",
        "main: fast-forwarded",
    ]
    assert git(clone, "rev-parse", "feature") == new
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "feature"
    assert git(clone, "status", "--porcelain") == ""

    (line, _) = app.sync(running(id), ["main"], [("feature", old, new)])
    assert line == "feature: matches the landed commits"
    extra = commit(clone, "feature", {"b": "2"})
    (line, _) = app.sync(running(id), ["main"], [("feature", old, new)])
    assert line == "feature: changed since it was fetched; left alone"
    assert git(clone, "rev-parse", "feature") == extra


def test_sync_rewrite_keeps_uncommitted_changes_in_place(
    project: Project, guest: tuple[App, str, Path]
) -> None:
    app, id, clone = guest
    old = commit(clone, "feature", {"a": "1"})
    (clone / "a").write_text("edited")
    new = advance(project)
    (line, _) = app.sync(running(id), ["main"], [("feature", old, new)])
    assert line.startswith("feature: left alone (uncommitted changes)")
    assert git(clone, "rev-parse", "feature") == old
    assert (clone / "a").read_text() == "edited"


def test_land_syncs_every_instance_of_the_project(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git(project.root, "config", "user.name", "Test")
    git(project.root, "config", "user.email", "test@example.invalid")
    store = Store(tmp_path / "state")
    engine = Guest(store)
    app = App(engine, store)
    first = app.new(project, harness, no_config=True)
    second = app.new(project, harness, no_config=True)
    idle = app.new(project, harness, no_config=True)
    engine.rows = [
        running(first),
        running(second),
        replace(running(idle), state="stopped"),
        running("devbox-unrelated"),
    ]
    clone = store.directory(first) / "repo"
    git(clone, "config", "user.name", "Test")
    git(clone, "config", "user.email", "test@example.invalid")
    other = store.directory(second) / "repo"
    git(other, "switch", "-qc", "scratch")

    # Another change lands first, so the first instance's branch is rebased by the landing.
    landed = advance(project)
    tip = commit(clone, "feature", {"a": "1"})
    dispatch(parser().parse_args(["land", first]), app)
    main = git(project.root, "rev-parse", "main")
    assert main != tip and git(project.root, "rev-parse", "main~1") == landed
    assert git(clone, "rev-parse", "feature") == main  # Rewritten to the rebased commit.
    assert git(clone, "rev-parse", "main") == main
    assert git(other, "rev-parse", "main") == main
    assert git(other, "symbolic-ref", "--short", "HEAD") == "scratch"
    out = capsys.readouterr().out
    assert f"{first}: feature: moved to the rebased commits" in out
    assert f"{first}: main: fast-forwarded" in out
    assert f"{second}: main: fast-forwarded" in out
    assert f"{idle}: stopped, not synced" in out
    assert "unrelated" not in out
    assert git(store.directory(idle) / "repo", "rev-parse", "main") != main


def test_land_sync_failure_does_not_undo_the_landing(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git(project.root, "config", "user.name", "Test")
    git(project.root, "config", "user.email", "test@example.invalid")
    store = Store(tmp_path / "state")
    engine = Guest(store)
    app = App(engine, store)
    id = app.new(project, harness, no_config=True)
    engine.rows = [running(id)]
    tip = commit(store.directory(id) / "repo", "feature", {"a": "1"})

    def broken(*args: Any, **kwargs: Any) -> None:
        raise DevboxError("container went away")

    monkeypatch.setattr(engine, "exec", broken)
    dispatch(parser().parse_args(["land", id]), app)
    assert git(project.root, "rev-parse", "main") == tip
    assert f"{id}: not synced: container went away" in capsys.readouterr().out
