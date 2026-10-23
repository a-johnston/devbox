import subprocess
from pathlib import Path

import pytest

from devbox.app import App
from devbox.cli import printable
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
    assert git(project.root, "rev-parse", "main") == host_main
    assert git(project.root, "tag") == ""


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


def test_fetch_reports_rewritten_and_pruned_branches(instance: tuple[App, str, Path]) -> None:
    app, id, clone = instance
    commit(clone, "feature", {"a": "1"})
    commit(clone, "gone", {"b": "1"})
    assert not any(branch.rewritten for branch in app.fetch(id))
    commit(clone, "feature", {"a": "2"})  # recreated from main: not a descendant
    git(clone, "checkout", "--quiet", "main")
    git(clone, "branch", "-D", "gone")
    fetched = {branch.ref.rsplit("/", 1)[1]: branch for branch in app.fetch(id)}
    assert set(fetched) == {"main", "feature"}
    assert fetched["feature"].rewritten


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
    with pytest.raises(DevboxError, match="no repository clone"):
        app.fetch(id)


def test_printable_escapes_terminal_controls() -> None:
    assert printable("refs/devbox/x/\x1b[2Jbad‮") == "refs/devbox/x/\\x1b[2Jbad\\u202e"
