import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import devbox.repository
from devbox import cli
from devbox.app import App, Landing
from devbox.cli import dispatch, parser
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.repository import Fetched, Git, HostCheckout, Landed, LocalBranches, landing_target, refs
from devbox.state import Store
from tests.fakes import FakeEngine, FakeGit
from tests.repos import commit, git


def publish(repository: Path, name: str, files: dict[str, str]) -> Fetched:
    """An instance branch as fetch publishes it: a tracking ref and a current local branch."""
    tip = commit(repository, f"x/{name}", files)
    git(repository, "checkout", "--quiet", "main")
    git(repository, "update-ref", f"refs/devbox/x/{name}", tip)
    return Fetched(f"refs/devbox/x/{name}", 1, False, (), f"x/{name}", True)


def test_land_rebases_onto_the_checked_out_branch_before_fast_forwarding(repository: Path) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    original = git(repository, "rev-parse", feature.ref)
    main = git(repository, "rev-parse", "main")
    host = commit(repository, "release", {"host": "new"})
    # Host configuration must not silently stash work or move other branches during rebase.
    git(repository, "config", "rebase.updateRefs", "true")
    git(repository, "branch", "keep", original)
    landed = Git(repository).land(feature)
    assert landed == Landed("release", original, git(repository, "rev-parse", "release"))
    assert git(repository, "branch", "--show-current") == "release"
    assert git(repository, "rev-parse", "release^", "main", "keep", feature.ref).split() == [
        host,
        main,
        original,
        original,
    ]
    assert "refs/heads/x/feature" not in refs(repository, "refs/heads/")
    assert (repository / "guest").read_text() == "new"
    assert (repository / "host").read_text() == "new"


def test_land_leaves_conflicting_rebase_for_the_user(repository: Path) -> None:
    feature = publish(repository, "feature", {"conflict": "guest"})
    original = git(repository, "rev-parse", feature.ref)
    host = commit(repository, "main", {"conflict": "host"})
    with pytest.raises(DevboxError, match="Rebase failed") as exc:
        Git(repository).land(feature)
    assert str(repository) in str(exc.value)
    assert "--continue" in str(exc.value)
    assert "--abort" in str(exc.value)
    assert "devbox sync" in str(exc.value)
    assert (repository / ".git/rebase-merge").is_dir()
    assert git(repository, "rev-parse", "main") == host
    assert "AA conflict" in git(repository, "status", "--porcelain")
    # A second invocation must not interfere with the unresolved rebase.
    with pytest.raises(DevboxError, match="already in progress"):
        Git(repository).land(feature)
    git(repository, "rebase", "--abort")
    assert git(repository, "branch", "--show-current") == "x/feature"
    assert git(repository, "rev-parse", "HEAD") == original


# landing_target: whether the host checkout's state allows a landing, and onto which branch.

TIP = "1" * 40
SOURCE = "refs/heads/x/feature"
FEATURE = Fetched("refs/devbox/x/feature", 1, False, (), "x/feature", True)


def checkout(**changes: Any) -> HostCheckout:
    clean = HostCheckout(
        Path("/host"),
        busy=False,
        head="refs/heads/main",
        dirty=False,
        branches=LocalBranches({SOURCE: TIP, "refs/heads/main": "0" * 40}, set(), {"refs/heads/main"}),
        tip=TIP,
    )
    return replace(clean, **changes)


def test_landing_target_is_the_checked_out_branch() -> None:
    assert landing_target(checkout(), FEATURE) == "main"
    assert landing_target(checkout(head="refs/heads/release"), FEATURE) == "release"


@pytest.mark.parametrize(
    ("state", "fetched", "reason"),
    [
        (checkout(), replace(FEATURE, branch_current=False), "unavailable or changed locally"),
        (checkout(), replace(FEATURE, branch=None), "unavailable or changed locally"),
        (checkout(busy=True), FEATURE, "already in progress"),
        (checkout(head=None), FEATURE, "check out a target branch"),
        (checkout(head=SOURCE), FEATURE, "onto itself"),
        (checkout(dirty=True), FEATURE, "must be clean"),
        (checkout(branches=LocalBranches({SOURCE: "2" * 40}, set(), set())), FEATURE, "no longer matches"),
        (checkout(branches=LocalBranches({}, {SOURCE}, set())), FEATURE, "no longer matches"),
        (checkout(branches=LocalBranches({}, set(), set())), FEATURE, "no longer matches"),
        (checkout(tip=None), FEATURE, "no longer matches"),
        (checkout(branches=LocalBranches({SOURCE: TIP}, set(), {SOURCE})), FEATURE, "checked out in a worktree"),
    ],
)
def test_landing_target_refuses_anything_that_could_lose_work(
    state: HostCheckout, fetched: Fetched, reason: str
) -> None:
    with pytest.raises(DevboxError, match=f"Cannot land.*{reason}"):
        landing_target(state, fetched)


def test_host_checkout_reads_a_clean_checkout(repository: Path) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    tip = git(repository, "rev-parse", feature.ref)
    state = HostCheckout.read(repository, feature.ref)
    assert (state.busy, state.head, state.dirty, state.tip) == (False, "refs/heads/main", False, tip)
    assert state.branches.commits["refs/heads/x/feature"] == tip


def test_host_checkout_reads_work_in_progress(repository: Path, tmp_path: Path) -> None:
    publish(repository, "feature", {"guest": "new"})
    git(repository, "worktree", "add", "--quiet", str(tmp_path / "linked"), "x/feature")
    git(repository, "checkout", "--quiet", "--detach")
    (repository / "untracked").write_text("local work")
    (repository / ".git/rebase-merge").mkdir()
    state = HostCheckout.read(repository, "refs/devbox/x/missing")
    assert (state.busy, state.head, state.dirty, state.tip) == (True, None, True, None)
    assert "refs/heads/x/feature" in state.branches.checked_out


def test_land_requires_ff_merge(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    guest = git(repository, "rev-parse", feature.ref)
    original_git = devbox.repository.git
    host = []

    def advance_before_merge(path, *args):
        if args[0] == "merge":
            host.append(commit(path, "main", {"concurrent": "new"}))
        return original_git(path, *args)

    monkeypatch.setattr(devbox.repository, "git", advance_before_merge)
    with pytest.raises(DevboxError, match="Not possible to fast-forward"):
        Git(repository).land(feature)
    assert git(repository, "rev-parse", "main") == host[0]
    assert git(repository, "rev-parse", "x/feature") == guest
    assert not (repository / "guest").exists()


# CLI: which fetched branch is handed to App.land, and what is printed.


def fetched(id: str, name: str, commits: int = 1) -> Fetched:
    branch = f"{id.removeprefix('devbox-')}/{name}" if commits else None
    return Fetched(f"refs/devbox/{id}/{name}", commits, False, (), branch, branch is not None)


@pytest.fixture
def selecting(project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[App, str]:
    """An instance whose fetch returns main and two changed branches."""
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit())
    id = app.new(project, harness, no_config=True)
    monkeypatch.setattr(app, "fetch", lambda i: [fetched(i, "main", 0), fetched(i, "one"), fetched(i, "two")])
    return app, id


def landings(app: App, monkeypatch: pytest.MonkeyPatch) -> list[Fetched]:
    landed: list[Fetched] = []

    def land(instance: str, branch: Fetched) -> Landing:
        landed.append(branch)
        return Landing(Landed("main", "old", "new"), [(instance, ["main: fast-forwarded", "\x1b[2J"])])

    monkeypatch.setattr(app, "land", land)
    return landed


def test_land_uses_pickers_and_prints_the_landing(
    selecting: tuple[App, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    app, id = selecting
    landed = landings(app, monkeypatch)
    prompts = []

    def pick(items, prompt, label):
        prompts.append(prompt)
        assert all(label(item) for item in items)
        return items[-1]

    monkeypatch.setattr(cli, "select", pick)
    dispatch(parser().parse_args(["land"]), app)
    assert prompts == ["Instances", "Branches"]
    assert [branch.ref for branch in landed] == [f"refs/devbox/{id}/two"]
    out = capsys.readouterr().out
    assert f"Landed {id.removeprefix('devbox-')}/two onto main.\n" in out
    assert f"{id}: main: fast-forwarded\n{id}: \\x1b[2J\n" in out


def test_land_selects_named_branches_without_a_picker(
    selecting: tuple[App, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, id = selecting
    landed = landings(app, monkeypatch)
    monkeypatch.setattr(cli, "select", lambda *a: pytest.fail("no picker needed"))
    with pytest.raises(DevboxError, match="matches 0 branches"):
        dispatch(parser().parse_args(["land", id, "missing"]), app)
    assert not landed
    dispatch(parser().parse_args(["land", id.removeprefix("devbox-"), "one"]), app)
    assert [branch.ref for branch in landed] == [f"refs/devbox/{id}/one"]


def test_land_without_new_work_fails(selecting: tuple[App, str], monkeypatch: pytest.MonkeyPatch) -> None:
    app, id = selecting
    monkeypatch.setattr(app, "fetch", lambda i: [fetched(i, "main", 0)])
    with pytest.raises(DevboxError, match="No branches have new commits"):
        dispatch(parser().parse_args(["land", id]), app)


# App.land: land in the instance's primary checkout, then sync every instance of its project.


def running(id: str) -> Container:
    return Container(id, "running", "project", "pi", "now")


@pytest.fixture
def project_instances(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[App, list[str], FakeGit, list[tuple]]:
    """Three instances of the project, the first started from a worktree; records the
    App syncs a landing would run."""
    engine = FakeEngine()
    repository = FakeGit()
    app = App(engine, Store(tmp_path / "state"), repository)
    worktree = project.root / ".worktrees/feature"
    worktree.mkdir(parents=True)
    ids = [
        app.new(replace(project, checkout=worktree, cwd=worktree), harness, no_config=True),
        *(app.new(project, harness, no_config=True) for _ in range(2)),
    ]
    elsewhere, directory = app.store.create()
    (directory / "metadata.json").write_text(json.dumps({"root": str(tmp_path / "elsewhere")}))
    first, second, idle = ids
    engine.rows = [
        running(first),
        running(second),
        replace(running(idle), state="stopped"),
        running(elsewhere),
        running("devbox-unrelated"),  # No saved state: not one of ours.
    ]
    synced: list[tuple] = []

    def sync(container: Container, branches: list[str], rewrites=()) -> list[str]:
        synced.append((container.id, branches, list(rewrites)))
        return [f"{container.id} synced"]

    monkeypatch.setattr(app, "sync", sync)
    return app, ids, repository, synced


def test_app_land_lands_in_the_primary_checkout_then_syncs_the_project(
    project: Project, project_instances: tuple[App, list[str], FakeGit, list[tuple]]
) -> None:
    app, (first, second, idle), repository, synced = project_instances
    branch = fetched(first, "feature/x")
    landing = app.land(first, branch)
    assert repository.calls == [("land", project.root, branch)]
    assert landing.landed == repository.landed == Landed("main", "fetched", "rebased")
    # Only the landing instance's branch moves to the rebased commits.
    assert synced == [
        (first, ["main"], [("feature/x", "fetched", "rebased")]),
        (second, ["main"], []),
    ]
    assert landing.synced == [
        (first, [f"{first} synced"]),
        (second, [f"{second} synced"]),
        (idle, ["stopped, not synced; run devbox sync later"]),
    ]


def test_app_land_reports_sync_failures_without_failing(
    project_instances: tuple[App, list[str], FakeGit, list[tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, (first, second, _), _, _ = project_instances
    recorded = app.sync

    def flaky(container: Container, branches: list[str], rewrites=()) -> list[str]:
        if container.id == first:
            raise DevboxError("container went away")
        return recorded(container, branches, rewrites)

    monkeypatch.setattr(app, "sync", flaky)
    assert app.land(first, fetched(first, "feature")).synced[:2] == [
        (first, ["not synced: container went away"]),
        (second, [f"{second} synced"]),
    ]

    def stopped() -> None:
        raise DevboxError("engine stopped")

    monkeypatch.setattr(app.engine, "require_running", stopped)
    assert app.land(first, fetched(first, "feature")).synced == [(first, ["not synced: engine stopped"])]


def test_app_land_failure_syncs_nothing(
    project_instances: tuple[App, list[str], FakeGit, list[tuple]],
) -> None:
    app, (first, *_), repository, synced = project_instances
    repository.landed = DevboxError("Rebase failed")
    with pytest.raises(DevboxError, match="Rebase failed"):
        app.land(first, fetched(first, "feature"))
    assert synced == []
