import subprocess
from pathlib import Path

import pytest

from devbox import repository
from devbox.app import App
from devbox.cli import dispatch, parser, printable
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=T", "-c", "user.email=t@e.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(clone: Path, branch: str, files: dict[str, str]) -> str:
    git(clone, "checkout", "--quiet", "-B", branch, "main")
    for name, text in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
    git(clone, "add", "--", *files)
    git(clone, "commit", "--quiet", "-m", f"change {branch}")
    return git(clone, "rev-parse", "HEAD")


@pytest.fixture
def instance(project: Project, harness: Harness, tmp_path: Path) -> tuple[App, str, Path]:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    return app, id, app.store.directory(id) / "repo"


def test_fetch_namespaces_branches_and_flags_trusted_paths(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    # Watched paths come from the host manifest; the clone's copy cannot remove them.
    with (project.root / ".devbox.toml").open("a") as stream:
        stream.write('[review]\nwatch = ["hooks/**/*.sh"]\n')
    host_main = git(project.root, "rev-parse", "main")
    plain = commit(clone, "plain", {"src/app.txt": "x"})
    commit(
        clone,
        "risky",
        {".devbox.toml": "version = 1\n", ".devbox/extra": "", "hooks/a/run.sh": "", "x.sh": ""},
    )
    fetched = {branch.ref: branch for branch in app.fetch(id)}
    assert set(fetched) == {f"refs/devbox/{id}/{name}" for name in ("main", "plain", "risky")}
    assert fetched[f"refs/devbox/{id}/plain"].commits == 1
    assert fetched[f"refs/devbox/{id}/plain"].watched == ()
    assert fetched[f"refs/devbox/{id}/main"].commits == 0
    assert fetched[f"refs/devbox/{id}/risky"].watched == (
        ".devbox.toml",
        ".devbox/extra",
        "hooks/a/run.sh",
    )
    assert git(project.root, "rev-parse", f"refs/devbox/{id}/plain") == plain
    for name in ("plain", "risky"):
        branch = fetched[f"refs/devbox/{id}/{name}"]
        assert branch.branch == f"{id.removeprefix('devbox-')}/{name}"
        assert branch.branch_current
        assert git(project.root, "rev-parse", f"refs/heads/{branch.branch}") == git(
            project.root, "rev-parse", branch.ref
        )
    assert fetched[f"refs/devbox/{id}/main"].branch is None
    assert set(repository.refs(project.root, "refs/heads/")) == {
        "refs/heads/main",
        *(f"refs/heads/{id.removeprefix('devbox-')}/{name}" for name in ("plain", "risky")),
    }
    assert git(project.root, "rev-parse", "main") == host_main
    assert git(project.root, "tag") == ""


def test_fetch_with_no_new_work_only_updates_tracking_refs(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    base = git(clone, "rev-parse", "main")
    git(clone, "branch", "untouched")
    for _ in range(2):
        fetched = app.fetch(id)
        assert {branch.ref for branch in fetched} == {
            f"refs/devbox/{id}/main",
            f"refs/devbox/{id}/untouched",
        }
        assert all(branch.commits == 0 and branch.branch is None for branch in fetched)
        assert not any(branch.branch_current for branch in fetched)
        assert repository.refs(project.root, "refs/heads/") == {"refs/heads/main": base}


def test_fetch_does_not_reset_a_published_branch_when_guest_drops_all_new_work(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    tip = commit(clone, "feature", {"a": "1"})
    published = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
    git(clone, "reset", "--hard", "main")
    branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
    assert branch.commits == 0
    assert branch.branch is None
    assert not branch.branch_current
    assert branch.rewritten
    assert git(project.root, "rev-parse", branch.ref) == git(clone, "rev-parse", "main")
    assert git(project.root, "rev-parse", f"refs/heads/{published.branch}") == tip


def test_fetch_publishes_main_when_it_has_new_work(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    base = git(project.root, "rev-parse", "main")
    tip = commit(clone, "main", {"a": "1"})
    (branch,) = app.fetch(id)
    assert branch.commits == 1
    assert branch.branch == f"{id.removeprefix('devbox-')}/main"
    assert branch.branch_current
    assert git(project.root, "rev-parse", f"refs/heads/{branch.branch}") == tip
    assert git(project.root, "rev-parse", "main") == base


def test_fetch_keeps_different_instances_branches_separate(
    project: Project, harness: Harness, instance: tuple[App, str, Path]
) -> None:
    app, first_id, first_clone = instance
    second_id = app.new(project, harness, no_config=True)
    second_clone = app.store.directory(second_id) / "repo"
    first = commit(first_clone, "feature", {"a": "1"})
    second = commit(second_clone, "feature", {"a": "2"})
    for id, tip in ((first_id, first), (second_id, second)):
        branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
        assert branch.branch_current
        assert branch.branch == f"{id.removeprefix('devbox-')}/feature"
        assert git(project.root, "rev-parse", f"refs/heads/{branch.branch}") == tip


def test_fetch_leaves_legacy_local_branches_untouched(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    first = commit(clone, "feature", {"a": "1"})
    app.fetch(id)
    legacy = f"devbox/{id}/feature"
    git(project.root, "branch", legacy, first)
    second = commit(clone, "feature", {"a": "2"})
    branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
    assert branch.branch_current
    assert git(project.root, "rev-parse", f"refs/heads/{branch.branch}") == second
    assert git(project.root, "rev-parse", f"refs/heads/{legacy}") == first


def test_fetch_from_a_worktree_watches_repository_relative_paths(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    tree = project.root / ".worktrees/feature"
    git(project.root, "worktree", "add", "-q", "-b", "feature", str(tree))
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(Project.discover(tree), harness, no_config=True)
    clone = app.store.directory(id) / "repo"
    git(clone, "checkout", "-q", "-b", "next")
    (clone / ".devbox/Containerfile").write_text("FROM other\n")
    git(clone, "commit", "-qam", "change")
    (branch,) = [b for b in app.fetch(id) if b.ref.endswith("/next")]
    assert branch.watched == (".devbox/Containerfile",)
    assert branch.branch_current
    assert git(project.root, "rev-parse", f"refs/heads/{branch.branch}") == git(
        clone, "rev-parse", "HEAD"
    )


def test_fetch_reports_rewritten_and_pruned_branches(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"a": "1"})
    gone = commit(clone, "gone", {"b": "1"})
    assert not any(branch.rewritten for branch in app.fetch(id))
    rewritten = commit(clone, "feature", {"a": "2"})  # recreated from main: not a descendant
    git(clone, "checkout", "--quiet", "main")
    git(clone, "branch", "-D", "gone")
    fetched = {branch.ref.rsplit("/", 1)[1]: branch for branch in app.fetch(id)}
    assert set(fetched) == {"main", "feature"}
    assert fetched["feature"].rewritten
    assert fetched["feature"].branch_current
    assert (
        git(project.root, "rev-parse", f"refs/heads/{id.removeprefix('devbox-')}/feature")
        == rewritten
    )
    # Prune tracking refs, never local branches that may hold reviewed or host work.
    assert git(project.root, "rev-parse", f"refs/heads/{id.removeprefix('devbox-')}/gone") == gone


def test_fetch_refreshes_untouched_local_branches(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    commit(clone, "feature/nested", {"a": "1"})
    app.fetch(id)
    git(clone, "commit", "--quiet", "--allow-empty", "-m", "next")
    tip = git(clone, "rev-parse", "HEAD")
    for _ in range(2):  # An unchanged fetch is also harmless.
        branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature/nested"))
        assert branch.branch_current
        assert git(project.root, "rev-parse", f"refs/heads/{branch.branch}") == tip


@pytest.mark.parametrize("linked", [False, True])
def test_fetch_does_not_move_checked_out_local_branches(
    project: Project, instance: tuple[App, str, Path], tmp_path: Path, linked: bool
) -> None:
    app, id, clone = instance
    first = commit(clone, "feature", {"a": "1"})
    app.fetch(id)
    name = f"{id.removeprefix('devbox-')}/feature"
    checkout = tmp_path / "review" if linked else project.root
    if linked:
        git(project.root, "worktree", "add", "--quiet", str(checkout), name)
    else:
        git(project.root, "checkout", "--quiet", name)
    (checkout / "a").write_text("uncommitted host edits")
    second = commit(clone, "feature", {"a": "2"})
    branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
    assert not branch.branch_current
    assert git(checkout, "rev-parse", "HEAD") == first
    assert (checkout / "a").read_text() == "uncommitted host edits"
    assert git(project.root, "rev-parse", branch.ref) == second


def test_fetch_preserves_locally_changed_branches_and_warns(
    project: Project, instance: tuple[App, str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"a": "1"})
    app.fetch(id)
    local = f"{id.removeprefix('devbox-')}/feature"
    host_main = git(project.root, "rev-parse", "main")
    git(project.root, "branch", "--force", local, host_main)
    tip = commit(clone, "feature", {"a": "2"})
    dispatch(parser().parse_args(["fetch", id]), app)
    assert git(project.root, "rev-parse", f"refs/heads/{local}") == host_main
    assert git(project.root, "rev-parse", f"refs/devbox/{id}/feature") == tip
    text = capsys.readouterr().out
    assert f"WARNING: local branch {local} not updated" in text
    assert f"use refs/devbox/{id}/feature" in text


@pytest.mark.parametrize("collision", ["existing", "prefix", "symbolic"])
def test_fetch_preserves_existing_host_refs_on_first_fetch(
    project: Project, instance: tuple[App, str, Path], collision: str
) -> None:
    app, id, clone = instance
    tip = commit(clone, "feature", {"a": "1"})
    name = f"{id.removeprefix('devbox-')}/feature"
    host_main = git(project.root, "rev-parse", "main")
    if collision == "symbolic":
        git(project.root, "symbolic-ref", f"refs/heads/{name}", "refs/heads/main")
    else:
        if collision == "prefix":
            name = id.removeprefix("devbox-")
        git(project.root, "branch", name, host_main)
    branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
    assert not branch.branch_current
    assert git(project.root, "rev-parse", f"refs/heads/{name}") == host_main
    assert git(project.root, "rev-parse", "main") == host_main
    assert git(project.root, "rev-parse", branch.ref) == tip
    if collision == "symbolic":
        assert git(project.root, "symbolic-ref", f"refs/heads/{name}") == "refs/heads/main"


@pytest.mark.parametrize("existing", [False, True])
def test_fetch_does_not_overwrite_a_concurrently_created_or_moved_branch(
    project: Project,
    instance: tuple[App, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    app, id, clone = instance
    tip = commit(clone, "feature", {"a": "1"})
    if existing:
        app.fetch(id)
        tip = commit(clone, "feature", {"a": "2"})
    local = f"refs/heads/{id.removeprefix('devbox-')}/feature"
    host_main = git(project.root, "rev-parse", "main")
    original = repository.git_output

    def race(directory: Path, *args: str) -> str | None:
        if args[:3] == ("update-ref", "--no-deref", local):
            git(project.root, "update-ref", local, host_main)
        return original(directory, *args)

    monkeypatch.setattr(repository, "git_output", race)
    branch = next(branch for branch in app.fetch(id) if branch.ref.endswith("/feature"))
    assert not branch.branch_current
    assert git(project.root, "rev-parse", local) == host_main
    assert git(project.root, "rev-parse", branch.ref) == tip


def test_fetch_never_runs_clone_configured_commands(
    instance: tuple[App, str, Path], tmp_path: Path
) -> None:
    app, id, clone = instance
    marker = tmp_path / "ran"
    commit(clone, "feature", {"a": "1"})
    for key in ("uploadpack.packObjectsHook", "core.fsmonitor", "core.hooksPath"):
        git(clone, "config", key, f"touch {marker}; false")
    assert app.fetch(id)
    assert not marker.exists()
    git(clone, "status")  # Host git inside the clone is NOT safe; fetch avoids it.
    assert marker.exists()


def test_fetch_refuses_a_redirected_git_directory(
    project: Project, instance: tuple[App, str, Path]
) -> None:
    app, id, clone = instance
    subprocess.run(["rm", "-rf", str(clone / ".git")], check=True)
    (clone / ".git").symlink_to(project.root / ".git")
    assert id not in app.store.clones()
    with pytest.raises(DevboxError, match="not a repository clone"):
        app.fetch(id)


def test_printable_escapes_terminal_controls() -> None:
    assert printable("refs/devbox/x/\x1b[2Jbad‮") == "refs/devbox/x/\\x1b[2Jbad\\u202e"
