import shutil
import subprocess
from pathlib import Path

import pytest

from agentbox.config import Harness, Project, harnesses
from agentbox.errors import AgentboxError


def test_self_hosting_manifest(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    shutil.copy(checkout / ".agentbox.toml", tmp_path)
    shutil.copytree(checkout / ".agentbox", tmp_path / ".agentbox")
    project = Project.discover(tmp_path)
    assert project.containerfile == tmp_path / ".agentbox/Containerfile"
    assert project.context == tmp_path / ".agentbox"
    assert project.artifacts == (".venv", ".pytest_cache", ".ruff_cache", "dist", ".worktrees")
    assert project.env == {"UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"}
    assert (project.cpus, project.memory, project.builder_memory) == (4, "8G", "4G")


def test_discovery_from_subdirectory(repository: Path) -> None:
    child = repository / "nested/deeper"
    child.mkdir(parents=True)
    project = Project.discover(child)
    assert project.root == repository
    assert project.cwd == child
    assert project.context == repository / ".agentbox"


def test_worktree_uses_primary_mount_and_falls_back_to_config(repository: Path) -> None:
    tree = repository / ".worktrees/feature"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "feature", str(tree)],
        check=True,
        capture_output=True,
    )
    (tree / ".agentbox.toml").unlink()
    project = Project.discover(tree)
    assert project.root == repository
    assert project.cwd == tree
    assert project.config == repository / ".agentbox.toml"


def test_discovery_ignores_inherited_git_hook_scope(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = repository / ".worktrees/feature"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "feature", str(tree)],
        check=True,
        capture_output=True,
    )
    monkeypatch.setenv("GIT_DIR", "/does/not/exist")
    monkeypatch.setenv("GIT_WORK_TREE", "/wrong/project")
    monkeypatch.setenv("GIT_INDEX_FILE", "/wrong/index")
    assert Project.discover(tree).root == repository


def test_worktree_hidden_by_artifact_rejected(repository: Path) -> None:
    config = repository / ".agentbox.toml"
    config.write_text(config.read_text().replace('".godot", "tests/bin"', '".godot", ".worktrees"'))
    tree = repository / ".worktrees/feature"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "feature", str(tree)],
        check=True,
        capture_output=True,
    )
    (tree / ".agentbox.toml").unlink()
    with pytest.raises(AgentboxError, match="hidden"):
        Project.discover(tree)


def test_external_worktree_rejected(repository: Path, tmp_path: Path) -> None:
    tree = tmp_path / "outside"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "outside", str(tree)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(AgentboxError, match="inside the primary"):
        Project.discover(tree)


@pytest.mark.parametrize("artifact", ["/tmp/cache", "../outside", ".git", ".git/objects", "."])
def test_unsafe_artifacts_rejected(repository: Path, artifact: str) -> None:
    config = repository / ".agentbox.toml"
    config.write_text(
        config.read_text().replace('".godot", "tests/bin", "tests/obj"', f'"{artifact}"')
    )
    with pytest.raises(AgentboxError, match="safe relative"):
        Project.discover(repository)


def test_overlapping_artifacts_rejected(repository: Path) -> None:
    config = repository / ".agentbox.toml"
    config.write_text(
        config.read_text().replace('".godot", "tests/bin", "tests/obj"', '"tests", "tests/obj"')
    )
    with pytest.raises(AgentboxError, match="overlap"):
        Project.discover(repository)


@pytest.mark.parametrize(
    "suffix",
    [
        "\n[container]\ncpus = 0\n",
        "\n[container]\ncpus = true\n",
        '\n[container]\nmemory = "garbage"\n',
        "\n[environment]\nX = 3\n",
        '\n[builder]\nmemory = "garbage"\n',
    ],
)
def test_invalid_resources_and_environment(repository: Path, suffix: str) -> None:
    with (repository / ".agentbox.toml").open("a") as stream:
        stream.write(suffix)
    with pytest.raises(AgentboxError):
        Project.discover(repository)


def test_builder_memory_is_independent(repository: Path) -> None:
    with (repository / ".agentbox.toml").open("a") as stream:
        stream.write('\n[builder]\nmemory = "2G"\n')
    project = Project.discover(repository)
    assert project.builder_memory == "2G"
    assert project.memory == "8G"


@pytest.mark.parametrize("path", [".pi/agent", ".pi/agent/sessions", ".pi/agent/sessions/cache"])
def test_prepared_dirs_cannot_cover_sessions(tmp_path: Path, harness: Harness, path: str) -> None:
    (tmp_path / "Containerfile").write_text("FROM ubuntu:24.04\n")
    (tmp_path / "profile.toml").write_text(
        (harness.directory / "profile.toml")
        .read_text()
        .replace(
            'prepared_dirs = [".pi/agent/npm", ".pi/agent/git"]', f'prepared_dirs = ["{path}"]'
        )
    )
    with pytest.raises(AgentboxError, match="overlap"):
        Harness.load(tmp_path)


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(AgentboxError, match="No .agentbox.toml"):
        Project.discover(tmp_path)


def test_custom_profile_overrides_builtin(
    tmp_path: Path, harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "agentbox/harnesses/pi"
    target.mkdir(parents=True)
    (target / "profile.toml").write_text(
        (harness.directory / "profile.toml")
        .read_text()
        .replace('command = ["pi",', 'command = ["custom-pi",')
    )
    (target / "Containerfile").write_text("ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert harnesses()["pi"].command[0] == "custom-pi"
