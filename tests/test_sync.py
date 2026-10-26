import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.repository import RECEIVE, HostGit, Started, bundle
from devbox.state import Session, Store
from tests.fakes import BASE, FakeEngine, FakeGit
from tests.repos import commit, git


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


def test_sync_bundles_host_branches_for_the_guest_script(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
    id = app.new(project, harness, no_config=True)
    bundled = []

    def bundle(root: Path, branches: list[str], base: str) -> str:
        bundled.append((root, branches, base))
        return "BUNDLE"

    monkeypatch.setattr("devbox.repository.bundle", bundle)
    engine.execs.clear()
    engine.stdout = "one\ntwo\n"
    # Rewrites go first: a rebased branch must not be reported as diverged from its target.
    assert app.sync(running(id), [], [("feature", "old", "new")]) == ["one", "two"]
    assert bundled == [(project.root, ["main"], BASE)]  # Defaults to the branch the instance started from.
    (request,) = engine.execs
    assert request.args == ("sh", "-c", RECEIVE, "sh", str(project.workspace), "rewrite:feature:old:new", "ff:main")
    assert request.input == "BUNDLE"

    # Instances created before the workspace move mount their clone at the host checkout.
    metadata = app.store.metadata(id)
    del metadata["workspace"]
    (app.store.directory(id) / "metadata.json").write_text(json.dumps(metadata))
    app.sync(running(id), ["release", "main"])
    assert bundled[-1] == (project.root, ["release", "main"], BASE)
    assert engine.execs[-1].args[4:] == (metadata["checkout"], "ff:release", "ff:main")
