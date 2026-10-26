import shutil
from pathlib import Path

import pytest

import devbox.repository
from devbox.app import App
from devbox.cli import printable
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.repository import Fetched, fetch, refs
from devbox.state import Store
from tests.fakes import FakeEngine, FakeGit
from tests.repos import commit, git

NAMESPACE = "refs/devbox/devbox-x/"


def fetched(repository: Path, clone: Path, *watched: str) -> dict[str, Fetched]:
    """Fetch as instance devbox-x, keyed by the clone's branch names."""
    result = fetch(repository, clone, NAMESPACE, watched, branch_prefix="x")
    return {branch.ref.removeprefix(NAMESPACE): branch for branch in result}


def test_fetch_namespaces_branches_and_flags_watched_paths(repository: Path, clone: Path) -> None:
    host_main = git(repository, "rev-parse", "main")
    plain = commit(clone, "plain", {"src/app.txt": "x"})
    commit(clone, "risky", {".devbox.toml": "version = 1\n", "hooks/a/run.sh": "", "x.sh": ""})
    result = fetched(repository, clone, ":(literal).devbox.toml", ":(glob)hooks/**/*.sh")
    assert set(result) == {"main", "plain", "risky"}
    assert (result["plain"].commits, result["plain"].watched) == (1, ())
    assert (result["main"].commits, result["main"].branch) == (0, None)
    assert result["risky"].watched == (".devbox.toml", "hooks/a/run.sh")
    assert git(repository, "rev-parse", f"{NAMESPACE}plain") == plain
    for name in ("plain", "risky"):
        assert result[name].branch == f"x/{name}"
        assert result[name].branch_current
        assert git(repository, "rev-parse", f"x/{name}") == git(repository, "rev-parse", result[name].ref)
    assert set(refs(repository, "refs/heads/")) == {"refs/heads/main", "refs/heads/x/plain", "refs/heads/x/risky"}
    assert git(repository, "rev-parse", "main") == host_main
    assert git(repository, "tag") == ""


def test_fetch_compares_with_the_current_host_head(repository: Path, clone: Path) -> None:
    # The clone predates a landed ancestor; only the unlanded commit and its changes count.
    landed = commit(clone, "main", {".devbox/Containerfile": "FROM prior\n"})
    git(clone, "checkout", "--quiet", "-b", "feature")
    git(clone, "commit", "--quiet", "--allow-empty", "-m", "unlanded")
    git(repository, "fetch", "--quiet", str(clone), "main")
    git(repository, "merge", "--quiet", "--ff-only", landed)
    commit(repository, "release", {"host-only": "not part of the review"})
    feature = fetched(repository, clone, ":(literal).devbox/Containerfile")["feature"]
    assert (feature.commits, feature.watched) == (1, ())


def test_fetch_already_landed_work_only_updates_tracking_refs(repository: Path, clone: Path) -> None:
    landed = commit(clone, "landed", {"a": "done"})
    fetched(repository, clone)
    git(repository, "merge", "--ff-only", landed)
    git(repository, "branch", "-d", "x/landed")
    for _ in range(2):
        result = fetched(repository, clone)
        assert set(result) == {"main", "landed"}
        assert all(b.commits == 0 and b.branch is None and not b.branch_current for b in result.values())
        assert refs(repository, "refs/heads/") == {"refs/heads/main": landed}


def test_fetch_does_not_reset_a_published_branch_when_guest_drops_all_new_work(repository: Path, clone: Path) -> None:
    tip = commit(clone, "feature", {"a": "1"})
    fetched(repository, clone)
    git(clone, "reset", "--hard", "main")
    branch = fetched(repository, clone)["feature"]
    assert (branch.commits, branch.branch, branch.branch_current, branch.rewritten) == (0, None, False, True)
    assert git(repository, "rev-parse", branch.ref) == git(clone, "rev-parse", "main")
    assert git(repository, "rev-parse", "x/feature") == tip


def test_fetch_publishes_main_when_it_has_new_work(repository: Path, clone: Path) -> None:
    base = git(repository, "rev-parse", "main")
    tip = commit(clone, "main", {"a": "1"})
    (branch,) = fetched(repository, clone).values()
    assert (branch.commits, branch.branch, branch.branch_current) == (1, "x/main", True)
    assert git(repository, "rev-parse", "x/main") == tip
    assert git(repository, "rev-parse", "main") == base


def test_fetch_keeps_different_instances_branches_separate(
    repository: Path, clone: Path, repository_template: Path, tmp_path: Path
) -> None:
    other = Path(shutil.copytree(repository_template, tmp_path / "other"))
    first = commit(clone, "feature", {"a": "1"})
    second = commit(other, "feature", {"a": "2"})
    for source, name in ((clone, "one"), (other, "two")):
        fetch(repository, source, f"refs/devbox/{name}/", (), branch_prefix=name)
    # Pruning one instance's namespace leaves the other's refs alone.
    assert git(repository, "rev-parse", "refs/devbox/one/feature", "one/feature").split() == [first] * 2
    assert git(repository, "rev-parse", "refs/devbox/two/feature", "two/feature").split() == [second] * 2


def test_fetch_leaves_unrelated_local_branches_untouched(repository: Path, clone: Path) -> None:
    first = commit(clone, "feature", {"a": "1"})
    fetched(repository, clone)
    legacy = "devbox/devbox-x/feature"
    git(repository, "branch", legacy, first)
    second = commit(clone, "feature", {"a": "2"})
    assert fetched(repository, clone)["feature"].branch_current
    assert git(repository, "rev-parse", "x/feature") == second
    assert git(repository, "rev-parse", f"refs/heads/{legacy}") == first


def test_fetch_reports_rewritten_and_pruned_branches(repository: Path, clone: Path) -> None:
    commit(clone, "feature", {"a": "1"})
    gone = commit(clone, "gone", {"b": "1"})
    assert not any(branch.rewritten for branch in fetched(repository, clone).values())
    rewritten = commit(clone, "feature", {"a": "2"})  # recreated from main: not a descendant
    git(clone, "checkout", "--quiet", "main")
    git(clone, "branch", "-D", "gone")
    result = fetched(repository, clone)
    assert set(result) == {"main", "feature"}
    assert result["feature"].rewritten
    assert result["feature"].branch_current
    assert git(repository, "rev-parse", "x/feature") == rewritten
    # Prune tracking refs, never local branches that may hold reviewed or host work.
    assert git(repository, "rev-parse", "x/gone") == gone


def test_fetch_refreshes_untouched_local_branches(repository: Path, clone: Path) -> None:
    commit(clone, "feature/nested", {"a": "1"})
    fetched(repository, clone)
    git(clone, "commit", "--quiet", "--allow-empty", "-m", "next")
    tip = git(clone, "rev-parse", "HEAD")
    for _ in range(2):  # An unchanged fetch is also harmless.
        assert fetched(repository, clone)["feature/nested"].branch_current
        assert git(repository, "rev-parse", "x/feature/nested") == tip


@pytest.mark.parametrize("linked", [False, True])
def test_fetch_does_not_move_checked_out_local_branches(
    repository: Path, clone: Path, tmp_path: Path, linked: bool
) -> None:
    first = commit(clone, "feature", {"a": "1"})
    fetched(repository, clone)
    checkout = tmp_path / "review" if linked else repository
    if linked:
        git(repository, "worktree", "add", "--quiet", str(checkout), "x/feature")
    else:
        git(repository, "checkout", "--quiet", "x/feature")
    (checkout / "a").write_text("uncommitted host edits")
    second = commit(clone, "feature", {"a": "2"})
    branch = fetched(repository, clone)["feature"]
    assert not branch.branch_current
    assert git(checkout, "rev-parse", "HEAD") == first
    assert (checkout / "a").read_text() == "uncommitted host edits"
    assert git(repository, "rev-parse", branch.ref) == second


def test_fetch_preserves_locally_changed_branches(repository: Path, clone: Path) -> None:
    commit(clone, "feature", {"a": "1"})
    fetched(repository, clone)
    host_main = git(repository, "rev-parse", "main")
    git(repository, "branch", "--force", "x/feature", host_main)
    tip = commit(clone, "feature", {"a": "2"})
    assert not fetched(repository, clone)["feature"].branch_current
    assert git(repository, "rev-parse", "x/feature") == host_main
    assert git(repository, "rev-parse", f"{NAMESPACE}feature") == tip


@pytest.mark.parametrize("collision", ["existing", "prefix", "symbolic"])
def test_fetch_preserves_existing_host_refs_on_first_fetch(repository: Path, clone: Path, collision: str) -> None:
    tip = commit(clone, "feature", {"a": "1"})
    name = "x" if collision == "prefix" else "x/feature"
    host_main = git(repository, "rev-parse", "main")
    if collision == "symbolic":
        git(repository, "symbolic-ref", f"refs/heads/{name}", "refs/heads/main")
    else:
        git(repository, "branch", name, host_main)
    branch = fetched(repository, clone)["feature"]
    assert not branch.branch_current
    assert git(repository, "rev-parse", f"refs/heads/{name}") == host_main
    assert git(repository, "rev-parse", "main") == host_main
    assert git(repository, "rev-parse", branch.ref) == tip
    if collision == "symbolic":
        assert git(repository, "symbolic-ref", f"refs/heads/{name}") == "refs/heads/main"


@pytest.mark.parametrize("existing", [False, True])
def test_fetch_does_not_overwrite_a_concurrently_created_or_moved_branch(
    repository: Path, clone: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    tip = commit(clone, "feature", {"a": "1"})
    if existing:
        fetched(repository, clone)
        tip = commit(clone, "feature", {"a": "2"})
    local = "refs/heads/x/feature"
    host_main = git(repository, "rev-parse", "main")
    original = devbox.repository.git_output

    def race(directory: Path, *args: str) -> str | None:
        if args[:3] == ("update-ref", "--no-deref", local):
            git(repository, "update-ref", local, host_main)
        return original(directory, *args)

    monkeypatch.setattr(devbox.repository, "git_output", race)
    branch = fetched(repository, clone)["feature"]
    assert not branch.branch_current
    assert git(repository, "rev-parse", local) == host_main
    assert git(repository, "rev-parse", branch.ref) == tip


def test_fetch_never_runs_clone_configured_commands(repository: Path, clone: Path, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    commit(clone, "feature", {"a": "1"})
    for key in ("uploadpack.packObjectsHook", "core.fsmonitor", "core.hooksPath"):
        git(clone, "config", key, f"touch {marker}; false")
    assert fetched(repository, clone)
    assert not marker.exists()
    git(clone, "status")  # Host git inside the clone is NOT safe; fetch avoids it.
    assert marker.exists()


def test_fetch_refuses_a_redirected_git_directory(repository: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "state")
    id, directory = store.create()
    (directory / "metadata.json").write_text("{}")
    (directory / "repo").mkdir()
    (directory / "repo/.git").symlink_to(repository / ".git")
    assert id not in store.clones()
    with pytest.raises(DevboxError, match="not a repository clone"):
        fetch(repository, directory / "repo", NAMESPACE, (), branch_prefix="x")


# App.fetch: which repository, clone, refs and paths an instance is fetched with.


def fetch_arguments(app: App, id: str, monkeypatch: pytest.MonkeyPatch) -> tuple:
    calls = []

    def record(*args: object, **kwargs: object) -> list[Fetched]:
        calls.append((*args, kwargs))
        return []

    monkeypatch.setattr(devbox.repository, "fetch", record)
    app.fetch(id)
    (call,) = calls
    return call


def test_app_fetch_watches_paths_from_the_host_manifest(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit())
    id = app.new(project, harness, no_config=True)
    # Watched paths come from the host manifest, never the clone's copy.
    with (project.root / ".devbox.toml").open("a") as stream:
        stream.write('[review]\nwatch = ["hooks/**/*.sh"]\n')
    assert fetch_arguments(app, id, monkeypatch) == (
        project.root,
        app.store.directory(id) / "repo",
        f"refs/devbox/{id}/",
        [":(literal).devbox.toml", ":(literal).devbox/Containerfile", ":(literal).devbox", ":(glob)hooks/**/*.sh"],
        {"branch_prefix": id.removeprefix("devbox-")},
    )


def test_app_fetch_from_a_worktree_watches_repository_relative_paths(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = project.root / ".worktrees/feature"
    git(project.root, "worktree", "add", "-q", "-b", "feature", str(tree))
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit())
    id = app.new(Project.discover(tree), harness, no_config=True)
    root, _, _, watched, _ = fetch_arguments(app, id, monkeypatch)
    assert root == project.root
    assert watched == [":(literal).devbox.toml", ":(literal).devbox/Containerfile", ":(literal).devbox"]


def test_printable_escapes_terminal_controls() -> None:
    assert printable("refs/devbox/x/\x1b[2Jbad‮") == "refs/devbox/x/\\x1b[2Jbad\\u202e"
