from pathlib import Path

import pytest

from devbox import cli, repository
from devbox.app import App
from devbox.cli import dispatch, parser
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine
from tests.test_fetch import commit, git


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
    monkeypatch.chdir(tmp_path)  # Selection, not cwd, determines the repository.
    monkeypatch.setattr(cli, "select", lambda *a: pytest.fail("no picker needed"))
    monkeypatch.setattr(app.engine, "require_running", lambda: pytest.fail("no engine needed"))
    dispatch(parser().parse_args(["land", id.removeprefix("devbox-")]), app)
    assert git(project.root, "rev-parse", "main") == tip
    assert git(project.root, "branch", "--show-current") == "main"
    assert git(project.root, "status", "--porcelain") == ""
    assert repository.refs(project.root, "refs/heads/") == {"refs/heads/main": tip}
    assert git(project.root, "rev-parse", f"refs/devbox/{id}/feature/x") == tip
    assert git(clone, "rev-parse", "feature/x") == tip
    assert f"Landed {id.removeprefix('devbox-')}/feature/x onto main." in capsys.readouterr().out


def test_land_rebases_before_fast_forwarding(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    original = commit(clone, "feature", {"guest": "new"})
    host = commit(project.root, "main", {"host": "new"})
    # Host configuration must not silently stash work or move other branches during rebase.
    git(project.root, "config", "rebase.updateRefs", "true")
    app.fetch(id)
    git(project.root, "branch", "keep", original)
    dispatch(parser().parse_args(["land", id, "feature"]), app)
    tip = git(project.root, "rev-parse", "main")
    assert tip != original
    assert git(project.root, "rev-parse", "main^") == host
    assert git(project.root, "rev-list", "--count", f"{host}..main") == "1"
    assert git(project.root, "rev-parse", "keep") == original
    assert f"refs/heads/{id.removeprefix('devbox-')}/feature" not in repository.refs(
        project.root, "refs/heads/"
    )
    assert git(project.root, "rev-parse", f"refs/devbox/{id}/feature") == original
    assert (project.root / "guest").read_text() == "new"
    assert (project.root / "host").read_text() == "new"
    assert git(clone, "rev-parse", "feature") == original


def test_land_targets_current_host_branch_not_starting_branch(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    base = git(project.root, "rev-parse", "main")
    commit(clone, "feature", {"guest": "new"})
    host = commit(project.root, "release", {"host": "new"})
    dispatch(parser().parse_args(["land", id]), app)
    assert git(project.root, "branch", "--show-current") == "release"
    assert git(project.root, "rev-parse", "release^") == host
    assert git(project.root, "rev-parse", "main") == base


def test_land_leaves_conflicting_rebase_for_the_user(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    original = commit(clone, "feature", {"conflict": "guest"})
    host = commit(project.root, "main", {"conflict": "host"})
    with pytest.raises(DevboxError, match="Rebase failed") as exc:
        dispatch(parser().parse_args(["land", id]), app)
    assert str(project.root) in str(exc.value)
    assert "--continue" in str(exc.value)
    assert "--abort" in str(exc.value)
    assert "devbox sync" in str(exc.value)
    assert (project.root / ".git/rebase-merge").is_dir()
    assert git(project.root, "rev-parse", "main") == host
    assert "AA conflict" in git(project.root, "status", "--porcelain")
    assert git(clone, "rev-parse", "feature") == original
    # A second invocation must not interfere with the unresolved rebase.
    with pytest.raises(DevboxError, match="already in progress"):
        dispatch(parser().parse_args(["land", id]), app)
    git(project.root, "rebase", "--abort")
    assert git(project.root, "branch", "--show-current") == f"{id.removeprefix('devbox-')}/feature"
    assert git(project.root, "rev-parse", "HEAD") == original


@pytest.mark.parametrize("dirty", ["staged", "unstaged", "untracked"])
def test_land_refuses_dirty_host_without_stashing(
    project: Project, instance: tuple[App, str, Path], dirty: str
) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"guest": "new"})
    base = git(project.root, "rev-parse", "HEAD")
    git(project.root, "config", "rebase.autoStash", "true")
    git(project.root, "config", "merge.autoStash", "true")
    path = project.root / ("untracked" if dirty == "untracked" else ".devbox/Containerfile")
    path.write_text("local edits")
    if dirty == "staged":
        git(project.root, "add", str(path))
    status = git(project.root, "status", "--porcelain")
    with pytest.raises(DevboxError, match="must be clean"):
        dispatch(parser().parse_args(["land", id]), app)
    assert git(project.root, "rev-parse", "HEAD") == base
    assert git(project.root, "branch", "--show-current") == "main"
    assert git(project.root, "status", "--porcelain") == status
    assert path.read_text() == "local edits"
    assert git(project.root, "stash", "list") == ""


def test_land_refuses_detached_target(project: Project, instance: tuple[App, str, Path]) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"guest": "new"})
    git(project.root, "checkout", "--detach")
    with pytest.raises(DevboxError, match="check out a target branch"):
        dispatch(parser().parse_args(["land", id]), app)


@pytest.mark.parametrize("changed", ["local", "linked", "self", "symbolic", "collision"])
def test_land_preserves_unavailable_local_branches(
    project: Project, instance: tuple[App, str, Path], tmp_path: Path, changed: str
) -> None:
    app, id, clone = instance
    tip = commit(clone, "feature", {"guest": "new"})
    (fetched,) = [b for b in app.fetch(id) if b.commits]
    assert fetched.branch is not None
    branch = fetched.branch
    base = git(project.root, "rev-parse", "main")
    if changed == "local":
        git(project.root, "branch", "-f", branch, base)
    elif changed == "linked":
        git(project.root, "worktree", "add", str(tmp_path / "linked"), branch)
    elif changed == "self":
        git(project.root, "checkout", branch)
    else:
        git(project.root, "branch", "-D", branch)
        if changed == "symbolic":
            git(project.root, "symbolic-ref", f"refs/heads/{branch}", "refs/heads/main")
        else:
            git(project.root, "branch", branch.split("/")[0])
    with pytest.raises(DevboxError, match="Cannot land"):
        dispatch(parser().parse_args(["land", id]), app)
    assert git(project.root, "rev-parse", "main") == base
    assert git(project.root, "rev-parse", fetched.ref) == tip
    if changed == "local":
        assert git(project.root, "rev-parse", branch) == base


def test_land_rechecks_branch_after_selection(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"guest": "new"})
    (fetched,) = [b for b in app.fetch(id) if b.commits]
    assert fetched.branch is not None
    git(project.root, "branch", "-f", fetched.branch, "main")
    with pytest.raises(DevboxError, match="no longer matches"):
        app.land(id, fetched)


def test_land_uses_instance_and_branch_pickers(
    project: Project, instance: tuple[App, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, id, clone = instance
    commit(clone, "one", {"one": "new"})
    tip = commit(clone, "two", {"two": "new"})
    prompts = []

    def pick(items, prompt, label):
        prompts.append(prompt)
        assert all(label(item) for item in items)
        return items[-1]

    monkeypatch.setattr(cli, "select", pick)
    dispatch(parser().parse_args(["land"]), app)
    assert prompts == ["Instances", "Branches"]
    assert git(project.root, "rev-parse", "main") == tip
    branches = repository.refs(project.root, "refs/heads/")
    assert f"refs/heads/{id.removeprefix('devbox-')}/one" in branches
    assert f"refs/heads/{id.removeprefix('devbox-')}/two" not in branches


def test_land_unknown_branch_does_not_merge(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    base = git(project.root, "rev-parse", "main")
    commit(clone, "feature", {"guest": "new"})
    with pytest.raises(DevboxError, match="matches 0 branches"):
        dispatch(parser().parse_args(["land", id, "missing"]), app)
    assert git(project.root, "rev-parse", "main") == base


def test_land_without_new_work_fails(instance: tuple[App, str, Path]) -> None:
    app, id, _ = instance
    with pytest.raises(DevboxError, match="unavailable"):
        dispatch(parser().parse_args(["land", id, "main"]), app)


def test_land_from_a_worktree_targets_primary_checkout(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
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


def test_land_never_runs_clone_hooks(
    project: Project, instance: tuple[App, str, Path], tmp_path: Path
) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"guest": "new"})
    marker = tmp_path / "hook-ran"
    hooks = clone / ".git/hooks"
    hook = hooks / "pre-rebase"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    dispatch(parser().parse_args(["land", id]), app)
    assert not marker.exists()
    assert (project.root / "guest").read_text() == "new"


def test_land_requires_ff_merge(
    project: Project, instance: tuple[App, str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, id, clone = instance
    guest = commit(clone, "feature", {"guest": "new"})
    original_git = repository.git
    host = []

    def advance_before_merge(path, *args):
        if args[0] == "merge":
            host.append(commit(path, "main", {"concurrent": "new"}))
        return original_git(path, *args)

    monkeypatch.setattr(repository, "git", advance_before_merge)
    with pytest.raises(DevboxError, match="Not possible to fast-forward"):
        dispatch(parser().parse_args(["land", id]), app)
    assert git(project.root, "rev-parse", "main") == host[0]
    assert git(project.root, "rev-parse", f"{id.removeprefix('devbox-')}/feature") == guest
    assert not (project.root / "guest").exists()
