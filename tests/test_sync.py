import base64
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
from devbox.repository import RECEIVE, Delivery, Git, Started, receive_specs
from devbox.state import Session, Store
from tests.fakes import BASE, FakeEngine, FakeRepository
from tests.repos import commit, git


def running(id: str) -> Container:
    return Container(id, "running", "project", "pi", "now")


# The guest side of sync: RECEIVE unpacks a host bundle and moves only what loses nothing.


def receive(
    repository: Path, clone: Path, branches: Sequence[str], rewrites: Sequence[tuple[str, str, str]] = ()
) -> list[str]:
    """Deliver host branches to clone as App.sync does, running the guest script locally."""
    delivery = Git().bundle(repository, branches, git(repository, "rev-list", "--max-parents=0", "main"))
    return run_receive(clone, receive_specs(delivery, branches, rewrites), delivery.data)


def run_receive(clone: Path, specs: Sequence[str], data: str = "") -> list[str]:
    result = subprocess.run(
        ["sh", "-c", RECEIVE, "sh", str(clone), *specs], input=data, text=True, check=True, capture_output=True
    )
    return result.stdout.splitlines()


def advance(repository: Path) -> str:
    git(repository, "commit", "--allow-empty", "-qm", "upstream")
    return git(repository, "rev-parse", "main")


def branches(repository: Path, **targets: str) -> None:
    """Create branches at the given commits in one Git process."""
    lines = "".join(f"create refs/heads/{name} {commit}\n" for name, commit in targets.items())
    subprocess.run(["git", "-C", str(repository), "update-ref", "--stdin"], input=lines, text=True, check=True)


def test_receive_reports_each_branch_independently(repository: Path, clone: Path) -> None:
    # Nothing here is checked out in the clone, so every outcome depends only on the refs.
    base = git(clone, "rev-parse", "HEAD")
    git(clone, "switch", "-qc", "work")
    guest = git(clone, "commit-tree", "-m", "guest work", "-p", base, f"{base}^{{tree}}")
    branches(clone, ahead=guest, diverged=guest, moved=guest, undelivered=guest, matches=base, changed=base, same=base)
    tip = advance(repository)
    host = git(repository, "commit-tree", "-m", "host work", "-p", base, f"{base}^{{tree}}")
    # Branches still at base travel by name beside the bundle of newer commits.
    branches(repository, other=tip, diverged=host, same=base, ahead=base)
    missing = "f" * 40
    rewrites = [
        ("moved", guest, tip),
        ("matches", guest, base),
        ("changed", guest, tip),
        ("gone", guest, tip),
        ("undelivered", guest, missing),
    ]
    assert receive(repository, clone, ["main", "other", "diverged", "same", "ahead"], rewrites) == [
        "moved: moved to the rebased commits",
        "matches: matches the landed commits",
        "changed: changed since it was fetched; left alone",
        "gone: no longer exists; left alone",
        "undelivered: rebased commits not delivered; left alone",
        "main: fast-forwarded",
        "other: delivered as host/other (no local branch)",
        "diverged: diverged from host/diverged; left alone",
        "same: up to date",
        "ahead: has commits beyond host/ahead; left alone",
    ]
    names = ("main", "moved", "changed", "undelivered", "ahead", "diverged", "host/other", "host/diverged", "host/same")
    assert git(clone, "rev-parse", *names).split() == [tip, tip, base, guest, guest, guest, tip, host, base]
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "work"


def test_receive_reports_a_known_commit_the_clone_lacks(clone: Path) -> None:
    assert run_receive(clone, [f"host:gone:{'f' * 40}", "ff:gone"]) == ["gone: not delivered"]


def test_receive_moves_a_clean_checked_out_branch(repository: Path, clone: Path) -> None:
    # The landing instance's own branch is usually checked out when it is rewritten.
    old = commit(clone, "feature", {"a": "1"})
    (clone / "untracked").write_text("kept")
    new = advance(repository)  # Stands in for the rebased commits, which main contains.
    assert receive(repository, clone, ["main"], [("feature", old, new)]) == [
        "feature: moved to the rebased commits",
        "main: fast-forwarded",
    ]
    assert git(clone, "rev-parse", "HEAD", "feature").split() == [new, new]
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "feature"
    assert git(clone, "status", "--porcelain") == "?? untracked"
    assert (clone / "untracked").read_text() == "kept"


@pytest.mark.parametrize(("busy", "reason"), [("edit", "uncommitted changes"), ("rebase", "a Git operation")])
def test_receive_leaves_a_busy_checked_out_branch(repository: Path, clone: Path, busy: str, reason: str) -> None:
    base = git(clone, "rev-parse", "HEAD")
    if busy == "edit":
        (clone / ".devbox.toml").write_text("edited")
    else:
        (clone / ".git/rebase-merge").mkdir()
    tip = advance(repository)
    (line,) = receive(repository, clone, ["main"])
    assert line == f"main: left alone ({reason}{' is in progress' if busy == 'rebase' else ''}); see host/main"
    assert git(clone, "rev-parse", "HEAD", "host/main").split() == [base, tip]
    if busy == "edit":
        assert (clone / ".devbox.toml").read_text() == "edited"


def test_bundle_delivers_branches_at_or_behind_base_by_name(repository: Path, tmp_path: Path) -> None:
    root = git(repository, "rev-parse", "main")
    base = advance(repository)
    git(repository, "branch", "at-base")
    git(repository, "branch", "behind", root)
    advance(repository)
    delivery = Git().bundle(repository, ["main", "at-base", "behind"], base)
    assert delivery.known == {"at-base": base, "behind": root}
    data = tmp_path / "data.bundle"
    data.write_bytes(base64.b64decode(delivery.data))
    heads = git(repository, "bundle", "list-heads", str(data)).splitlines()
    assert [line.split()[1] for line in heads] == ["refs/heads/main"]
    # Nothing beyond base: no bundle at all, rather than the branches' whole history.
    assert Git().bundle(repository, ["at-base"], base) == Delivery("", {"at-base": base})


def test_receive_specs_deliver_then_rewrite_then_fast_forward() -> None:
    # A rebased branch must not be reported as diverged from its target.
    delivery = Delivery("", {"release": "a" * 40})
    assert receive_specs(delivery, ["main", "release"], [("feature", "old", "new")]) == [
        f"host:release:{'a' * 40}",
        "rewrite:feature:old:new",
        "ff:main",
        "ff:release",
    ]


def test_bundle_refuses_missing_branches(repository: Path) -> None:
    with pytest.raises(DevboxError, match="No branch missing"):
        Git().bundle(repository, ["missing"], git(repository, "rev-parse", "main"))


# Instances: what they are told, where forks start, and which branches App.sync delivers.


def test_instance_learns_its_identity_and_constraints(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())
    id = app.new(project, harness, no_config=True)
    assert engine.runs[0].env["DEVBOX_INSTANCE"] == id
    text = (app.store.directory(id) / "home/.devbox/environment.md").read_text()
    assert f"{project.workspace} is a private clone of the user's repository, started from main" in text
    assert f"`devbox review {id}`" in text and f"`devbox sync {id} [branch ...]`" in text


def test_clone_instance_continues_from_its_checked_out_branch(clone: Path, tmp_path: Path) -> None:
    work = commit(clone, "work", {"a": "1"})
    target = tmp_path / "fork"
    assert Git().clone_instance(clone, target) == "work"
    assert git(target, "rev-parse", "HEAD") == work
    assert git(target, "symbolic-ref", "--short", "HEAD") == "work"


def test_fork_from_instance_keeps_the_original_host_starting_point(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeRepository(Started("release", BASE, dirty=False)))
    first = app.new(project, harness, no_config=True)
    source = app.store.directory(first) / "home/source.jsonl"
    source.write_text('{"type":"session","id":"s"}\n')
    session = Session(f"{first}/s", first, "pi", str(project.cwd), source)
    app.repository = FakeRepository(Started("main", "f" * 40, dirty=False))

    resumed = app.new(project, harness, no_config=True, session=session, from_instance=True)
    metadata = app.store.metadata(resumed)
    assert (metadata["branch"], metadata["base"]) == ("release", BASE)
    text = (app.store.directory(resumed) / "home/.devbox/environment.md").read_text()
    assert "started from copied" in text  # What the fake's clone_instance checked out.

    fresh = app.new(project, harness, no_config=True, session=session)
    assert (app.store.metadata(fresh)["branch"], app.store.metadata(fresh)["base"]) == ("main", "f" * 40)


def test_sync_refusals(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeRepository())
    id = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="stopped"):
        app.sync(replace(running(id), state="stopped"), [])
    app.repository = FakeRepository(Started(None, BASE, dirty=False))
    detached = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="name branches"):
        app.sync(running(detached), [])


def test_sync_bundles_host_branches_for_the_guest_script(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    repository = FakeRepository()
    app = App(engine, Store(tmp_path / "state"), repository)
    id = app.new(project, harness, no_config=True)
    repository.delivery = Delivery("BUNDLE", {"release": BASE})
    engine.execs.clear()
    engine.stdout = "one\ntwo\n"
    assert app.sync(running(id), [], [("feature", "old", "new")]) == ["one", "two"]
    # Defaults to the branch the instance started from.
    assert repository.calls == [("bundle", project.root, ["main"], BASE)]
    (request,) = engine.execs
    specs = receive_specs(repository.delivery, ["main"], [("feature", "old", "new")])
    assert request.args == ("sh", "-c", RECEIVE, "sh", str(project.workspace), *specs)
    assert request.input == "BUNDLE"

    # Instances created before the workspace move mount their clone at the host checkout.
    metadata = app.store.metadata(id)
    del metadata["workspace"]
    (app.store.directory(id) / "metadata.json").write_text(json.dumps(metadata))
    app.sync(running(id), ["release", "main"])
    assert repository.calls[-1] == ("bundle", project.root, ["release", "main"], BASE)
    assert engine.execs[-1].args[4] == metadata["checkout"]
