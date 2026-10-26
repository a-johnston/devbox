import json
import subprocess
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
from devbox.repository import Fetched, Landed, land, refs
from devbox.state import Store
from tests.fakes import FakeEngine, FakeGit
from tests.repos import commit, git


def publish(repository: Path, name: str, files: dict[str, str]) -> Fetched:
    """An instance branch as fetch publishes it: a tracking ref and a current local branch."""
    tip = commit(repository, f"x/{name}", files)
    git(repository, "checkout", "--quiet", "main")
    git(repository, "update-ref", f"refs/devbox/x/{name}", tip)
    return Fetched(f"refs/devbox/x/{name}", 1, False, (), f"x/{name}", True)


def test_land_rebases_before_fast_forwarding(repository: Path) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    original = git(repository, "rev-parse", feature.ref)
    host = commit(repository, "main", {"host": "new"})
    # Host configuration must not silently stash work or move other branches during rebase.
    git(repository, "config", "rebase.updateRefs", "true")
    git(repository, "branch", "keep", original)
    assert land(repository, feature) == Landed("main", original, git(repository, "rev-parse", "main"))
    assert git(repository, "rev-parse", "main^") == host
    assert git(repository, "rev-list", "--count", f"{host}..main") == "1"
    assert git(repository, "rev-parse", "keep") == original
    assert "refs/heads/x/feature" not in refs(repository, "refs/heads/")
    assert git(repository, "rev-parse", feature.ref) == original
    assert (repository / "guest").read_text() == "new"
    assert (repository / "host").read_text() == "new"


def test_land_targets_the_current_host_branch(repository: Path) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    main = git(repository, "rev-parse", "main")
    host = commit(repository, "release", {"host": "new"})
    assert land(repository, feature).target == "release"
    assert git(repository, "branch", "--show-current") == "release"
    assert git(repository, "rev-parse", "release^") == host
    assert git(repository, "rev-parse", "main") == main


def test_land_leaves_conflicting_rebase_for_the_user(repository: Path) -> None:
    feature = publish(repository, "feature", {"conflict": "guest"})
    original = git(repository, "rev-parse", feature.ref)
    host = commit(repository, "main", {"conflict": "host"})
    with pytest.raises(DevboxError, match="Rebase failed") as exc:
        land(repository, feature)
    assert str(repository) in str(exc.value)
    assert "--continue" in str(exc.value)
    assert "--abort" in str(exc.value)
    assert "devbox sync" in str(exc.value)
    assert (repository / ".git/rebase-merge").is_dir()
    assert git(repository, "rev-parse", "main") == host
    assert "AA conflict" in git(repository, "status", "--porcelain")
    # A second invocation must not interfere with the unresolved rebase.
    with pytest.raises(DevboxError, match="already in progress"):
        land(repository, feature)
    git(repository, "rebase", "--abort")
    assert git(repository, "branch", "--show-current") == "x/feature"
    assert git(repository, "rev-parse", "HEAD") == original


@pytest.mark.parametrize("dirty", ["staged", "unstaged", "untracked"])
def test_land_refuses_dirty_host_without_stashing(repository: Path, dirty: str) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "config", "rebase.autoStash", "true")
    git(repository, "config", "merge.autoStash", "true")
    path = repository / ("untracked" if dirty == "untracked" else ".devbox/Containerfile")
    path.write_text("local edits")
    if dirty == "staged":
        git(repository, "add", str(path))
    status = git(repository, "status", "--porcelain")
    with pytest.raises(DevboxError, match="must be clean"):
        land(repository, feature)
    assert git(repository, "rev-parse", "HEAD") == base
    assert git(repository, "branch", "--show-current") == "main"
    assert git(repository, "status", "--porcelain") == status
    assert path.read_text() == "local edits"
    assert git(repository, "stash", "list") == ""


def test_land_refuses_detached_target(repository: Path) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    git(repository, "checkout", "--quiet", "--detach")
    with pytest.raises(DevboxError, match="check out a target branch"):
        land(repository, feature)


def test_land_refuses_a_branch_fetch_could_not_publish(repository: Path) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    for unavailable in (replace(feature, branch_current=False), replace(feature, branch=None)):
        with pytest.raises(DevboxError, match="unavailable or changed locally"):
            land(repository, unavailable)


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ("local", "no longer matches"),
        ("symbolic", "no longer matches"),
        ("collision", "no longer matches"),
        ("linked", "checked out in a worktree"),
        ("self", "onto itself"),
    ],
)
def test_land_rechecks_the_branch_after_selection(repository: Path, tmp_path: Path, changed: str, reason: str) -> None:
    feature = publish(repository, "feature", {"guest": "new"})
    tip = git(repository, "rev-parse", feature.ref)
    base = git(repository, "rev-parse", "main")
    if changed == "local":
        git(repository, "branch", "-f", "x/feature", base)
    elif changed == "linked":
        git(repository, "worktree", "add", "--quiet", str(tmp_path / "linked"), "x/feature")
    elif changed == "self":
        git(repository, "checkout", "--quiet", "x/feature")
    else:
        git(repository, "branch", "-D", "x/feature")
        if changed == "symbolic":
            git(repository, "symbolic-ref", "refs/heads/x/feature", "refs/heads/main")
        else:
            git(repository, "branch", "x")
    with pytest.raises(DevboxError, match=f"Cannot land.*{reason}"):
        land(repository, feature)
    assert git(repository, "rev-parse", "main") == base
    assert git(repository, "rev-parse", feature.ref) == tip


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
        land(repository, feature)
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
    with pytest.raises(DevboxError, match="unavailable"):
        dispatch(parser().parse_args(["land", id, "main"]), app)
    with pytest.raises(DevboxError, match="No branches have new commits"):
        dispatch(parser().parse_args(["land", id]), app)


# App.land: land in the instance's primary checkout, then sync every instance of its project.


def running(id: str) -> Container:
    return Container(id, "running", "project", "pi", "now")


@pytest.fixture
def project_instances(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[App, list[str], list[tuple], list[tuple]]:
    """Three instances of the project, the first started from a worktree; records the
    repository landings and App syncs it would run."""
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
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
    landed: list[tuple] = []
    synced: list[tuple] = []

    def land(root: Path, branch: Fetched) -> Landed:
        landed.append((root, branch))
        return Landed("main", "fetched", "rebased")

    def sync(container: Container, branches: list[str], rewrites=()) -> list[str]:
        synced.append((container.id, branches, list(rewrites)))
        return [f"{container.id} synced"]

    monkeypatch.setattr(devbox.repository, "land", land)
    monkeypatch.setattr(app, "sync", sync)
    return app, ids, landed, synced


def test_app_land_lands_in_the_primary_checkout_then_syncs_the_project(
    project: Project, project_instances: tuple[App, list[str], list[tuple], list[tuple]]
) -> None:
    app, (first, second, idle), landed, synced = project_instances
    branch = fetched(first, "feature/x")
    landing = app.land(first, branch)
    assert landed == [(project.root, branch)]
    assert landing.landed == Landed("main", "fetched", "rebased")
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
    project_instances: tuple[App, list[str], list[tuple], list[tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, (first, second, _), _, synced = project_instances
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
    project_instances: tuple[App, list[str], list[tuple], list[tuple]], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, (first, *_), _, synced = project_instances

    def conflict(root: Path, branch: Fetched) -> Landed:
        raise DevboxError("Rebase failed")

    monkeypatch.setattr(devbox.repository, "land", conflict)
    with pytest.raises(DevboxError, match="Rebase failed"):
        app.land(first, fetched(first, "feature"))
    assert synced == []


# End to end with real Git: one compound landing that rebases, then syncs two running instances.


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


def test_land_rebases_and_syncs_running_instances(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git(project.root, "config", "user.name", "Test")
    git(project.root, "config", "user.email", "test@example.invalid")
    store = Store(tmp_path / "state")
    engine = Guest(store)
    app = App(engine, store)
    first, second = (app.new(project, harness, no_config=True) for _ in range(2))
    engine.rows = [running(first), running(second)]
    clone, other = (store.directory(id) / "repo" for id in (first, second))
    git(other, "switch", "-qc", "scratch")
    # Landing never runs Git in the clone, so its hooks never run on the host.
    marker = tmp_path / "hook-ran"
    hook = clone / ".git/hooks/pre-rebase"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)

    # Another change lands first, so the instance's branch is rebased by the landing.
    git(project.root, "commit", "--allow-empty", "-qm", "upstream")
    upstream = git(project.root, "rev-parse", "main")
    tip = commit(clone, "feature", {"a": "1"})
    monkeypatch.chdir(tmp_path)  # Selection, not cwd, determines the repository.
    dispatch(parser().parse_args(["land", first.removeprefix("devbox-")]), app)

    main = git(project.root, "rev-parse", "main")
    assert main != tip and git(project.root, "rev-parse", "main~1") == upstream
    assert (git(project.root, "branch", "--show-current"), git(project.root, "status", "--porcelain")) == ("main", "")
    assert refs(project.root, "refs/heads/") == {"refs/heads/main": main}
    assert git(project.root, "rev-parse", f"refs/devbox/{first}/feature") == tip
    assert git(clone, "rev-parse", "feature", "main").split() == [main, main]  # Rewritten, then fast-forwarded.
    assert git(other, "rev-parse", "main") == main
    assert git(other, "symbolic-ref", "--short", "HEAD") == "scratch"
    assert not marker.exists()
    out = capsys.readouterr().out
    assert f"Landed {first.removeprefix('devbox-')}/feature onto main." in out
    assert f"{first}: feature: moved to the rebased commits" in out
    assert f"{first}: main: fast-forwarded" in out
    assert f"{second}: main: fast-forwarded" in out
