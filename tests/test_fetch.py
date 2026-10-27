import shutil
from pathlib import Path

import pytest

from devbox.app import App
from devbox.cli import printable
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.repository import Fetched, Git, LocalBranches, Tip, compare, publish, refs, update_branch
from devbox.state import Store
from tests.fakes import FakeEngine, FakeGit
from tests.repos import commit, git

NAMESPACE = "refs/devbox/devbox-x/"


def fetched(repository: Path, clone: Path, *watched: str) -> dict[str, Fetched]:
    """Fetch as instance devbox-x, keyed by the clone's branch names."""
    result = Git(repository).fetch(clone, NAMESPACE, watched, branch_prefix="x")
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
    # Prune tracking refs, never local branches that may hold reviewed or host work.
    git(clone, "branch", "-D", "plain")
    assert set(fetched(repository, clone)) == {"main", "risky"}
    assert git(repository, "rev-parse", "x/plain") == plain


def test_compare_measures_a_tip_against_the_host_head(repository: Path, clone: Path) -> None:
    # The clone predates a landed ancestor and a host-only commit; only unlanded work counts.
    landed = commit(clone, "main", {".devbox/Containerfile": "FROM prior\n"})
    first = commit(clone, "feature", {"notes.txt": "1"})
    git(clone, "commit", "--quiet", "--allow-empty", "-m", "more")
    tip = git(clone, "rev-parse", "HEAD")
    git(clone, "branch", "earlier")
    rewritten = commit(clone, "feature", {"notes.txt": "2"})  # Recreated from main: not a descendant.
    git(repository, "fetch", "--quiet", str(clone), "+refs/heads/*:refs/test/*")
    git(repository, "merge", "--quiet", "--ff-only", landed)
    commit(repository, "release", {"host-only": "not part of the review"})
    head = git(repository, "rev-parse", "HEAD")
    watched = (":(literal).devbox/Containerfile", ":(glob)*.txt")
    assert compare(repository, head, "refs/t", tip, first, watched) == Tip(
        "refs/t", tip, first, 2, True, ("notes.txt",)
    )
    assert compare(repository, head, "refs/t", rewritten, tip, watched).descends is False
    assert compare(repository, head, "refs/t", tip, None, watched).descends is True


# publish: whether fetch moves, leaves or already has each fetched tip's local branch.

OLD, NEW, ELSE = "1" * 40, "2" * 40, "3" * 40
REF = "refs/heads/x/feature"


def local(
    commits: dict[str, str] | None = None, symbolic: set[str] | None = None, checked_out: set[str] | None = None
) -> LocalBranches:
    return LocalBranches(commits or {}, symbolic or set(), checked_out or set())


@pytest.mark.parametrize(
    ("case", "old", "branches", "current", "expected"),
    [
        ("first fetch creates it", None, local(), True, "0" * 40),
        ("refreshed while untouched", OLD, local({REF: OLD}), True, OLD),
        ("already at the tip", OLD, local({REF: NEW}, checked_out={REF}), True, None),
        ("changed locally", OLD, local({REF: ELSE}), False, None),
        ("existing host branch on first fetch", None, local({REF: ELSE}), False, None),
        ("checked out in a worktree", OLD, local({REF: OLD}, checked_out={REF}), False, None),
        ("symbolic", OLD, local({"refs/heads/main": OLD}, symbolic={REF}), False, None),
    ],
)
def test_publish_moves_only_untouched_local_branches(
    case: str, old: str | None, branches: LocalBranches, current: bool, expected: str | None
) -> None:
    tip = Tip(f"{NAMESPACE}feature", NEW, old, 2, True, (".devbox.toml",))
    assert publish(tip, NAMESPACE, "x", branches) == (
        Fetched(tip.ref, 2, False, (".devbox.toml",), "x/feature", current),
        expected,
    )


@pytest.mark.parametrize("descends", [True, False])
def test_publish_leaves_branches_without_new_work_alone(descends: bool) -> None:
    # Already landed, or the guest dropped all new work: an existing local branch keeps it.
    tip = Tip(f"{NAMESPACE}feature/nested", NEW, OLD, 0, descends, ())
    branches = local({"refs/heads/x/feature/nested": OLD})
    assert publish(tip, NAMESPACE, "x", branches) == (Fetched(tip.ref, 0, not descends, (), None, False), None)


def test_publish_names_branches_under_the_prefix() -> None:
    for name in ("main", "feature/nested"):
        fetched, _ = publish(Tip(NAMESPACE + name, NEW, None, 1, True, ()), NAMESPACE, "x", local())
        assert fetched.branch == f"x/{name}"


def test_fetch_keeps_different_instances_branches_separate(
    repository: Path, clone: Path, repository_template: Path, tmp_path: Path
) -> None:
    other = Path(shutil.copytree(repository_template, tmp_path / "other"))
    first = commit(clone, "feature", {"a": "1"})
    second = commit(other, "feature", {"a": "2"})
    for source, name in ((clone, "one"), (other, "two")):
        Git(repository).fetch(source, f"refs/devbox/{name}/", (), branch_prefix=name)
    # Pruning one instance's namespace leaves the other's refs alone.
    assert git(repository, "rev-parse", "refs/devbox/one/feature", "one/feature").split() == [first] * 2
    assert git(repository, "rev-parse", "refs/devbox/two/feature", "two/feature").split() == [second] * 2


def test_local_branches_reads_symbolic_and_checked_out_branches(repository: Path, tmp_path: Path) -> None:
    main = git(repository, "rev-parse", "main")
    git(repository, "branch", "linked")
    git(repository, "worktree", "add", "--quiet", str(tmp_path / "linked"), "linked")
    git(repository, "symbolic-ref", "refs/heads/alias", "refs/heads/main")
    assert LocalBranches.read(repository) == LocalBranches(
        {"refs/heads/main": main, "refs/heads/linked": main},
        {"refs/heads/alias"},
        {"refs/heads/main", "refs/heads/linked"},
    )


def test_fetch_leaves_a_colliding_host_branch_intact(repository: Path, clone: Path) -> None:
    # An existing x branch blocks creating x/feature; the update fails without moving anything.
    host_main = git(repository, "rev-parse", "main")
    git(repository, "branch", "x")
    tip = commit(clone, "feature", {"a": "1"})
    branch = fetched(repository, clone)["feature"]
    assert (branch.branch, branch.branch_current) == ("x/feature", False)
    assert git(repository, "rev-parse", "x", branch.ref).split() == [host_main, tip]


def test_update_branch_compares_and_swaps(repository: Path) -> None:
    main = git(repository, "rev-parse", "main")
    new = git(repository, "commit-tree", "-m", "new", "-p", main, "main^{tree}")
    zero = "0" * 40
    git(repository, "branch", "x/moved")
    git(repository, "branch", "y")
    assert update_branch(repository, "x/created", new, zero)
    assert not update_branch(repository, "x/created", main, zero)  # Created since it was read.
    assert not update_branch(repository, "x/moved", main, new)  # Moved since it was read.
    assert not update_branch(repository, "y/feature", new, zero)  # Collides with the y branch.
    assert update_branch(repository, "x/moved", new, main)
    assert git(repository, "rev-parse", "x/created", "x/moved", "y").split() == [new, new, main]


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
        Git(repository).fetch(directory / "repo", NAMESPACE, (), branch_prefix="x")


# App.fetch: which repository, clone, refs and paths an instance is fetched with.


def fetch_arguments(app: App, id: str) -> tuple[object, ...]:
    assert isinstance(app.git, FakeGit)
    app.fetch(id)
    (call,) = [call[1:] for call in app.git.calls if call[0] == "fetch"]
    return call


def test_app_fetch_watches_paths_from_the_host_manifest(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit())
    id = app.new(project, harness, no_config=True)
    # Watched paths come from the host manifest, never the clone's copy.
    with (project.root / ".devbox.toml").open("a") as stream:
        stream.write('[review]\nwatch = ["hooks/**/*.sh"]\n')
    assert fetch_arguments(app, id) == (
        project.root,
        app.store.directory(id) / "repo",
        f"refs/devbox/{id}/",
        [":(literal).devbox.toml", ":(literal).devbox/Containerfile", ":(literal).devbox", ":(glob)hooks/**/*.sh"],
        id.removeprefix("devbox-"),
    )


def test_app_fetch_from_a_worktree_watches_repository_relative_paths(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    tree = project.root / ".worktrees/feature"
    git(project.root, "worktree", "add", "-q", "-b", "feature", str(tree))
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeGit())
    id = app.new(Project.discover(tree), harness, no_config=True)
    root, _, _, watched, _ = fetch_arguments(app, id)
    assert root == project.root
    assert watched == [":(literal).devbox.toml", ":(literal).devbox/Containerfile", ":(literal).devbox"]


def test_printable_escapes_terminal_controls() -> None:
    assert printable("refs/devbox/x/\x1b[2Jbad‮") == "refs/devbox/x/\\x1b[2Jbad\\u202e"
