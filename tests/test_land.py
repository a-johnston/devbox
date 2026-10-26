from dataclasses import replace
from pathlib import Path

import pytest

import devbox.repository
from devbox import cli
from devbox.app import App
from devbox.cli import dispatch, parser
from devbox.config import Harness, Project
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


# Selection: which fetched branch the CLI hands to App.land.


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

    def land(instance: str, branch: Fetched) -> Landed:
        landed.append(branch)
        return Landed("main", "old", "new")

    monkeypatch.setattr(app, "land", land)
    return landed


def test_land_uses_instance_and_branch_pickers(
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
    assert f"Landed {id.removeprefix('devbox-')}/two onto main." in capsys.readouterr().out


def test_land_selects_named_branches(selecting: tuple[App, str], monkeypatch: pytest.MonkeyPatch) -> None:
    app, id = selecting
    landed = landings(app, monkeypatch)
    with pytest.raises(DevboxError, match="matches 0 branches"):
        dispatch(parser().parse_args(["land", id, "missing"]), app)
    assert not landed
    dispatch(parser().parse_args(["land", id, "one"]), app)
    assert [branch.ref for branch in landed] == [f"refs/devbox/{id}/one"]


def test_land_without_new_work_fails(selecting: tuple[App, str], monkeypatch: pytest.MonkeyPatch) -> None:
    app, id = selecting
    monkeypatch.setattr(app, "fetch", lambda i: [fetched(i, "main", 0)])
    with pytest.raises(DevboxError, match="unavailable"):
        dispatch(parser().parse_args(["land", id, "main"]), app)
    with pytest.raises(DevboxError, match="No branches have new commits"):
        dispatch(parser().parse_args(["land", id]), app)


# App composition with real Git: fetch, land in the primary checkout, then sync.


@pytest.fixture
def instance(project: Project, harness: Harness, tmp_path: Path) -> tuple[App, str, Path]:
    git(project.root, "config", "user.name", "Test")
    git(project.root, "config", "user.email", "test@example.invalid")
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    return app, id, app.store.directory(id) / "repo"


def test_land_fetches_and_fast_forwards_without_a_picker(
    project: Project,
    instance: tuple[App, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app, id, clone = instance
    tip = commit(clone, "feature/x", {"new": "instance work"})
    # Landing never runs Git in the clone, so its hooks never run on the host.
    marker = tmp_path / "hook-ran"
    hook = clone / ".git/hooks/pre-rebase"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    monkeypatch.chdir(tmp_path)  # Selection, not cwd, determines the repository.
    monkeypatch.setattr(cli, "select", lambda *a: pytest.fail("no picker needed"))
    dispatch(parser().parse_args(["land", id.removeprefix("devbox-")]), app)
    assert git(project.root, "rev-parse", "main") == tip
    assert git(project.root, "branch", "--show-current") == "main"
    assert git(project.root, "status", "--porcelain") == ""
    assert refs(project.root, "refs/heads/") == {"refs/heads/main": tip}
    assert git(project.root, "rev-parse", f"refs/devbox/{id}/feature/x") == tip
    assert git(clone, "rev-parse", "feature/x") == tip
    assert f"Landed {id.removeprefix('devbox-')}/feature/x onto main." in capsys.readouterr().out
    assert not marker.exists()


def test_land_from_a_worktree_targets_primary_checkout(project: Project, harness: Harness, tmp_path: Path) -> None:
    (project.root / ".git/info/exclude").write_text(".worktrees/\n")
    tree = project.root / ".worktrees/feature"
    git(project.root, "worktree", "add", "-q", "-b", "feature", str(tree))
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(Project.discover(tree), harness, no_config=True)
    clone = app.store.directory(id) / "repo"
    git(clone, "checkout", "-b", "main")
    tip = commit(clone, "next", {"guest": "new"})
    dispatch(parser().parse_args(["land", id]), app)
    assert git(project.root, "rev-parse", "main") == tip
    assert git(tree, "branch", "--show-current") == "feature"
    assert not (tree / "guest").exists()
