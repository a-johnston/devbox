import shutil
from pathlib import Path

import pytest

import devbox.repository
from devbox.app import App
from devbox.cli import printable
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.repository import Fetched, LocalBranches, Tip, fetch, publish, refs
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
        fetch(repository, source, f"refs/devbox/{name}/", (), branch_prefix=name)
    # Pruning one instance's namespace leaves the other's refs alone.
    assert git(repository, "rev-parse", "refs/devbox/one/feature", "one/feature").split() == [first] * 2
    assert git(repository, "rev-parse", "refs/devbox/two/feature", "two/feature").split() == [second] * 2


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
