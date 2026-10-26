import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from devbox import repository
from devbox.app import COLORS, PALETTE, App, accent, tmux_config
from devbox.config import Harness, Project
from devbox.engine import Container, mount
from devbox.errors import DevboxError
from devbox.state import Session, Store
from tests.fakes import FakeEngine


def test_new_composes_images_and_mounts_only_selected_paths(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    assert len(engine.builds) == 3
    run = engine.runs[0]
    clone = app.store.directory(id) / "repo"
    assert run.binds == (
        (str(clone), "/workspace/project with spaces"),
        (str(app.store.directory(id) / "home"), "/home/agent"),
    )
    assert run.cpus == project.cpus
    assert run.memory == project.memory
    assert run.image == engine.builds[-1].tag
    (startup,) = engine.execs
    assert startup.id == id
    assert startup.args == (
        "bash",
        "/home/agent/.devbox/harness.sh",
        "start",
        "/workspace/project with spaces",
        "0",
        *harness.command,
    )
    home = app.store.directory(id) / "home"
    assert (home / ".pi/agent/npm").readlink() == Path("/opt/devbox/prepared/.pi/agent/npm")
    assert "prefix C-a" in (home / ".devbox/tmux.conf").read_text()


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=T", "-c", "user.email=t@e.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_clone_copies_committed_branch_without_host_links(
    project: Project, harness: Harness, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / ".devbox.toml").write_text("dirty")
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    clone = app.store.directory(id) / "repo"
    assert "Uncommitted changes" in capsys.readouterr().out
    assert git(clone, "symbolic-ref", "HEAD") == "refs/heads/main"
    assert git(clone, "rev-parse", "HEAD") == git(project.root, "rev-parse", "HEAD")
    assert "dirty" not in (clone / ".devbox.toml").read_text()
    assert git(clone, "remote") == ""
    assert git(clone, "rev-parse", "refs/remotes/origin/main")
    assert not (clone / ".git/objects/info/alternates").exists()
    packs = [p for p in (clone / ".git/objects").rglob("*") if p.is_file()]
    assert packs and all(p.stat().st_nlink == 1 for p in packs)
    metadata = app.store.metadata(id)
    assert (metadata["branch"], metadata["base"]) == ("main", git(clone, "rev-parse", "HEAD"))


def test_clone_follows_a_detached_worktree(project: Project, harness: Harness, tmp_path: Path) -> None:
    tree = project.root / ".worktrees/loose"
    git(project.root, "worktree", "add", "--detach", str(tree))
    git(tree, "commit", "--allow-empty", "-m", "unreachable from branches")
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(Project.discover(tree), harness, no_config=True)
    clone = app.store.directory(id) / "repo"
    assert git(clone, "rev-parse", "HEAD") == git(tree, "rev-parse", "HEAD")
    assert app.store.metadata(id)["branch"] is None
    # Named after the primary checkout, not the worktree.
    assert engine.runs[0].binds[0] == (str(clone), "/workspace/project with spaces")


def test_non_repository_projects_are_rejected(tmp_path: Path, harness: Harness, project: Project) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(DevboxError, match="Git repository"):
        App(FakeEngine(), Store(tmp_path / "state")).new(
            replace(project, root=plain, cwd=plain, checkout=plain), harness, no_config=True
        )


def test_git_identity_is_passed_as_environment(project: Project, harness: Harness, tmp_path: Path) -> None:
    for key, value in (("user.name", "Ada"), ("user.email", "ada@example.invalid")):
        subprocess.run(["git", "-C", str(project.root), "config", key, value], check=True)
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, harness, no_config=True)
    env = engine.runs[0].env
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Ada"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "ada@example.invalid"
    assert not any("credential" in key.lower() for key in env)


def test_fork_snapshots_conversation(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    original_id, original = app.store.create()
    (original / "metadata.json").write_text("{}")
    source = original / "home/source.jsonl"
    source.write_text('{"type":"session","id":"original"}\n')
    session = Session(f"{original_id}/original", original_id, "pi", str(project.cwd), source)
    id = app.new(project, harness, no_config=True, session=session)
    home = app.store.directory(id) / "home"
    assert (home / ".devbox/resume.jsonl").read_text() == source.read_text()
    assert engine.execs[-1].args[-2:] == ("--fork", "/home/agent/.devbox/resume.jsonl")


def test_failed_start_removes_container_but_retains_state(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    execute = engine.exec

    def fail_tmux(id: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "start" in args:
            raise DevboxError("startup failed")
        return execute(id, *args, **kwargs)

    monkeypatch.setattr(engine, "exec", fail_tmux)
    with pytest.raises(DevboxError, match="startup failed"):
        app.new(project, harness, no_config=True)
    assert engine.removed == [engine.runs[0].name]
    assert len(list(app.store.instances.glob("*/metadata.json"))) == 1


def test_attach_connects_directly_to_tmux(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path))
    id, directory = app.store.create()
    (directory / "metadata.json").write_text("{}")
    container = Container(id, "running", "project", "pi", "now")
    app.attach(container)
    assert engine.connections == [(id, ("tmux", "-u", "attach-session", "-t", "agent"), None)]


@pytest.mark.parametrize(
    ("metadata", "cwd"),
    [
        (
            {"project": "/host/project", "workdir": "/workspace/project/sub"},
            "/workspace/project/sub",
        ),
        # Instances created before the workspace move mount their clone at the host path.
        ({"project": "/host/project"}, "/host/project"),
    ],
)
def test_shell_starts_in_the_guest_workdir(tmp_path: Path, metadata: dict[str, str], cwd: str) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path))
    id, directory = app.store.create()
    (directory / "metadata.json").write_text(json.dumps(metadata))
    app.shell(Container(id, "running", "project", "pi", "now"))
    assert engine.connections == [(id, ("/bin/bash",), cwd)]


def test_kill_all_retains_state(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [Container("ours", "running", "project", "pi", "now")]
    app = App(engine, Store(tmp_path))
    id, directory = app.store.create()
    (directory / "metadata.json").write_text("{}")
    assert app.kill_all() == 1
    assert (app.store.directory(id) / "metadata.json").exists()
    assert engine.removed == ["ours"]


def test_accent_is_stable_and_status_cannot_inject_tmux_commands() -> None:
    assert accent("id") == accent("id")
    assert len({accent(str(i)) for i in range(30)}) > 4
    config = tmux_config("id", "#(touch /host/pwned)'", "pi")
    assert "#(touch" not in config
    assert "prefix C-a" in config
    assert "set -g mouse on" in config
    assert "status-style" in config
    assert "' p | pi | id '" in tmux_config("id", "p", "pi")


def test_instances_are_named_after_free_colors_then_fall_back(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    first = app.new(project, harness, no_config=True)
    second = app.new(project, harness, no_config=True)
    assert first != second
    color = first.removeprefix("devbox-")
    assert {color, second.removeprefix("devbox-")} <= COLORS.keys()
    home = app.store.directory(first) / "home"
    assert f"bg=colour{COLORS[color]}," in (home / ".devbox/tmux.conf").read_text()
    for color in COLORS:
        (app.store.instances / f"devbox-{color}").mkdir(exist_ok=True)
    fallback = app.new(project, harness, no_config=True)
    assert fallback.removeprefix("devbox-") not in COLORS
    assert accent(fallback) in PALETTE


@pytest.mark.parametrize("path", ["/a,b", "/a\nb"])
def test_mount_rejects_cli_delimiters(path: str) -> None:
    with pytest.raises(DevboxError):
        mount(Path(path), "/workspace")


def test_colorterm_is_inherited_unless_overridden(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("SECRET_TOKEN", "hunter2")
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    app.new(project, harness, no_config=True)
    assert engine.runs[-1].env["COLORTERM"] == "truecolor"
    assert "SECRET_TOKEN" not in engine.runs[-1].env
    app.new(replace(project, env={"COLORTERM": "24bit"}), harness, no_config=True)
    assert engine.runs[-1].env["COLORTERM"] == "24bit"


def test_builds_and_describes_the_host_architecture(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    assert [build.platform for build in engine.builds] == ["linux/amd64"] * 3
    text = (app.store.directory(id) / "home/.devbox/environment.md").read_text()
    assert f"devbox instance {id}, a Linux AMD64 container, as a non-root user." in text
    assert "Do not assume host tools" in text


def test_clone_seeds_lfs_objects_from_the_source(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oid = "ab" + "cd" * 31
    # A stand-in for git-lfs: only the listing, as the clone has no remote to download from.
    tool = tmp_path / "bin/git-lfs"
    tool.parent.mkdir()
    tool.write_text(f"#!/bin/sh\necho '{oid} * data/file.bin'\necho '{'f' * 64} * missing.bin'\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tool.parent}:{os.environ['PATH']}")
    stored = project.root / ".git/lfs/objects/ab/cd" / oid
    stored.parent.mkdir(parents=True)
    stored.write_bytes(b"payload")
    target = tmp_path / "clone"
    repository.clone(project.root, target)
    assert (target / ".git/lfs/objects/ab/cd" / oid).read_bytes() == b"payload"
    assert not (target / ".git/lfs/objects/ff").exists()
