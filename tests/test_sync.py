import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from devbox.app import App
from devbox.cli import dispatch, parser
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.repository import RECEIVE, HostGit, Started, bundle
from devbox.state import Session, Store
from tests.fakes import BASE, FakeEngine, FakeGit
from tests.repos import commit, git


class Guest(FakeEngine):
    """Runs piped exec scripts locally, with the guest checkout path mapped to the clone."""

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

    def exec(self, id: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = super().exec(id, *args, **kwargs)
        if kwargs.get("input") is None:
            return result
        checkout = self.store.metadata(id)["workspace"]
        clone = str(self.store.directory(id) / "repo")
        command = [clone if arg == checkout else arg for arg in args]
        return subprocess.run(command, input=kwargs["input"], text=True, check=True, capture_output=True)


def running(id: str) -> Container:
    return Container(id, "running", "project", "pi", "now")


# The guest side of sync: RECEIVE unpacks a host bundle and moves only what loses nothing.


def receive(
    repository: Path, clone: Path, branches: Sequence[str], rewrites: Sequence[tuple[str, str, str]] = ()
) -> list[str]:
    """Deliver host branches to clone as App.sync does, running the guest script locally."""
    base = git(repository, "rev-list", "--max-parents=0", "main")
    specs = [f"rewrite:{name}:{old}:{new}" for name, old, new in rewrites] + [f"ff:{b}" for b in branches]
    result = subprocess.run(
        ["sh", "-c", RECEIVE, "sh", str(clone), *specs],
        input=bundle(repository, branches, base),
        text=True,
        check=True,
        capture_output=True,
    )
    return result.stdout.splitlines()


def advance(repository: Path) -> str:
    git(repository, "commit", "--allow-empty", "-qm", "upstream")
    return git(repository, "rev-parse", "main")


def test_receive_delivers_host_branches_as_host_refs(repository: Path, clone: Path) -> None:
    assert receive(repository, clone, ["main"]) == ["main: up to date"]  # nothing new since the clone
    tip = advance(repository)
    git(repository, "branch", "other")
    assert receive(repository, clone, ["main", "other"]) == [
        "main: fast-forwarded",
        "other: delivered as host/other (no local branch)",
    ]
    assert git(clone, "rev-parse", "host/main", "host/other").split() == [tip, tip]
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "main"
    assert git(clone, "rev-parse", "main") == tip


def test_bundle_refuses_missing_branches(repository: Path) -> None:
    with pytest.raises(DevboxError, match="No branch missing"):
        bundle(repository, ["missing"], git(repository, "rev-parse", "main"))


def test_receive_fast_forwards_an_idle_local_branch(repository: Path, clone: Path) -> None:
    git(clone, "switch", "-qc", "feature")
    tip = advance(repository)
    assert receive(repository, clone, ["main"]) == ["main: fast-forwarded"]
    assert git(clone, "rev-parse", "main") == tip
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "feature"


def test_receive_fast_forwards_the_clean_checked_out_branch(repository: Path, clone: Path) -> None:
    (clone / "untracked").write_text("kept")
    tip = advance(repository)
    assert receive(repository, clone, ["main"]) == ["main: fast-forwarded"]
    assert git(clone, "rev-parse", "HEAD") == tip
    assert (clone / "untracked").read_text() == "kept"


def test_receive_leaves_a_checked_out_branch_with_uncommitted_changes(repository: Path, clone: Path) -> None:
    base = git(clone, "rev-parse", "HEAD")
    (clone / ".devbox.toml").write_text("edited")
    tip = advance(repository)
    (line,) = receive(repository, clone, ["main"])
    assert line.startswith("main: left alone (uncommitted changes)")
    assert git(clone, "rev-parse", "HEAD") == base
    assert git(clone, "rev-parse", "host/main") == tip
    assert (clone / ".devbox.toml").read_text() == "edited"


def test_receive_leaves_a_branch_during_a_git_operation(repository: Path, clone: Path) -> None:
    base = git(clone, "rev-parse", "HEAD")
    (clone / ".git/rebase-merge").mkdir()
    advance(repository)
    (line,) = receive(repository, clone, ["main"])
    assert "a Git operation is in progress" in line
    assert git(clone, "rev-parse", "HEAD") == base


def test_receive_leaves_diverged_and_ahead_branches(repository: Path, clone: Path) -> None:
    ahead = commit(clone, "ahead", {"a": "1"})
    side = git(repository, "commit-tree", "-m", "host side", "-p", "main", "main^{tree}")
    git(repository, "update-ref", "refs/heads/ahead", side)
    assert receive(repository, clone, ["ahead"]) == ["ahead: diverged from host/ahead; left alone"]
    git(repository, "update-ref", "refs/heads/ahead", "main")
    assert receive(repository, clone, ["ahead"]) == ["ahead: has commits beyond host/ahead; left alone"]
    assert git(clone, "rev-parse", "ahead") == ahead


def test_receive_rewrites_a_branch_only_while_it_is_unchanged(repository: Path, clone: Path) -> None:
    old = commit(clone, "feature", {"a": "1"})
    new = advance(repository)  # Stands in for the rebased commits, which main contains.
    assert receive(repository, clone, ["main"], [("feature", old, new)]) == [
        "feature: moved to the rebased commits",
        "main: fast-forwarded",
    ]
    assert git(clone, "rev-parse", "feature") == new
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "feature"
    assert git(clone, "status", "--porcelain") == ""

    (line, _) = receive(repository, clone, ["main"], [("feature", old, new)])
    assert line == "feature: matches the landed commits"
    extra = commit(clone, "feature", {"b": "2"})
    (line, _) = receive(repository, clone, ["main"], [("feature", old, new)])
    assert line == "feature: changed since it was fetched; left alone"
    assert git(clone, "rev-parse", "feature") == extra


def test_receive_rewrite_keeps_uncommitted_changes_in_place(repository: Path, clone: Path) -> None:
    old = commit(clone, "feature", {"a": "1"})
    (clone / "a").write_text("edited")
    new = advance(repository)
    (line, _) = receive(repository, clone, ["main"], [("feature", old, new)])
    assert line.startswith("feature: left alone (uncommitted changes)")
    assert git(clone, "rev-parse", "feature") == old
    assert (clone / "a").read_text() == "edited"


# Instances: what they are told, where forks start, and which branches App.sync delivers.


def test_instance_learns_its_identity_and_constraints(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
    id = app.new(project, harness, no_config=True)
    assert engine.runs[0].env["DEVBOX_INSTANCE"] == id
    text = (app.store.directory(id) / "home/.devbox/environment.md").read_text()
    assert f"{project.workspace} is a private clone of the user's repository, started from main" in text
    assert f"`devbox review {id}`" in text and f"`devbox sync {id} [branch ...]`" in text


def test_clone_instance_continues_from_its_checked_out_branch(clone: Path, tmp_path: Path) -> None:
    work = commit(clone, "work", {"a": "1"})
    target = tmp_path / "fork"
    assert HostGit().clone_instance(clone, target) == "work"
    assert git(target, "rev-parse", "HEAD") == work
    assert git(target, "symbolic-ref", "--short", "HEAD") == "work"


def test_fork_from_instance_keeps_the_original_host_starting_point(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit(Started("release", BASE, dirty=False)))
    first = app.new(project, harness, no_config=True)
    source = app.store.directory(first) / "home/source.jsonl"
    source.write_text('{"type":"session","id":"s"}\n')
    session = Session(f"{first}/s", first, "pi", str(project.cwd), source)
    app.git = FakeGit(Started("main", "f" * 40, dirty=False))

    resumed = app.new(project, harness, no_config=True, session=session, from_instance=True)
    metadata = app.store.metadata(resumed)
    assert (metadata["branch"], metadata["base"]) == ("release", BASE)
    text = (app.store.directory(resumed) / "home/.devbox/environment.md").read_text()
    assert "started from copied" in text  # What the fake's clone_instance checked out.

    fresh = app.new(project, harness, no_config=True, session=session)
    assert (app.store.metadata(fresh)["branch"], app.store.metadata(fresh)["base"]) == ("main", "f" * 40)


def test_sync_refusals(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit())
    id = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="stopped"):
        app.sync(replace(running(id), state="stopped"), [])
    app.git = FakeGit(Started(None, BASE, dirty=False))
    detached = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="name branches"):
        app.sync(running(detached), [])


def test_sync_delivers_the_starting_branch_to_the_instance(project: Project, harness: Harness, tmp_path: Path) -> None:
    store = Store(tmp_path / "state")
    app = App(Guest(store), store)
    id = app.new(project, harness, no_config=True)
    clone = store.directory(id) / "repo"
    assert app.sync(running(id), []) == ["main: up to date"]
    assert git(clone, "rev-parse", "host/main") == store.metadata(id)["base"]
    tip = advance(project.root)
    git(project.root, "branch", "other")
    assert app.sync(running(id), ["other"]) == ["other: delivered as host/other (no local branch)"]
    assert app.sync(running(id), []) == ["main: fast-forwarded"]
    assert git(clone, "rev-parse", "main") == tip


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
    other = store.directory(second) / "repo"
    git(other, "switch", "-qc", "scratch")

    # Another change lands first, so the first instance's branch is rebased by the landing.
    landed = advance(project.root)
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
