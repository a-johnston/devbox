import re
import shutil
import subprocess
from pathlib import Path

import pytest

from devbox.config import Harness, Project, harnesses, init
from devbox.errors import DevboxError


def test_self_hosting_manifest(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    shutil.copy(checkout / ".devbox.toml", tmp_path)
    shutil.copytree(checkout / ".devbox", tmp_path / ".devbox")
    project = Project.discover(tmp_path)
    assert project.containerfile == tmp_path / ".devbox/Containerfile"
    assert project.context == tmp_path / ".devbox"
    assert project.env == {"UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"}
    assert (project.cpus, project.memory) == (4, "8G")
    assert project.watch == ("prek.toml",)


@pytest.mark.parametrize(
    "watch",
    ['"prek.toml"', '["/etc/x"]', '["../x"]', '[".git"]', '[".git/objects"]', '[""]', "[1]"],
)
def test_invalid_review_watch_rejected(repository: Path, watch: str) -> None:
    with (repository / ".devbox.toml").open("a") as stream:
        stream.write(f"\n[review]\nwatch = {watch}\n")
    with pytest.raises(DevboxError):
        Project.discover(repository)


def test_init_template_documents_every_option(tmp_path: Path) -> None:
    created = init(tmp_path)
    assert created == {tmp_path / ".devbox.toml": True, tmp_path / ".devbox/Containerfile": True}
    assert init(tmp_path) == dict.fromkeys(created, False)
    config = tmp_path / ".devbox.toml"
    # Uncomment every example section and setting; each must parse and take effect.
    config.write_text(re.sub(r"(?m)^# (?=\[|\w+ = )", "", config.read_text()))
    project = Project.discover(tmp_path)
    assert (project.cpus, project.memory) == (4, "8G")
    assert project.env and project.watch


def test_discovery_from_subdirectory(repository: Path) -> None:
    child = repository / "nested/deeper"
    child.mkdir(parents=True)
    project = Project.discover(child)
    assert project.root == repository
    assert project.cwd == child
    assert project.context == repository / ".devbox"
    assert str(project.workdir) == f"/workspace/{repository.name}/nested/deeper"


def test_worktree_uses_primary_mount_and_falls_back_to_config(repository: Path) -> None:
    tree = repository / ".worktrees/feature"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "feature", str(tree)],
        check=True,
        capture_output=True,
    )
    (tree / ".devbox.toml").unlink()
    project = Project.discover(tree)
    assert project.root == repository
    assert project.cwd == tree
    assert project.config == repository / ".devbox.toml"


def test_discovery_ignores_inherited_git_hook_scope(repository: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_worktree_is_its_own_checkout(repository: Path) -> None:
    tree = repository / ".worktrees/feature"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "feature", str(tree)],
        check=True,
        capture_output=True,
    )
    (tree / ".devbox.toml").unlink()
    (tree / "sub").mkdir()
    project = Project.discover(tree / "sub")
    assert project.checkout == tree
    assert str(project.workdir) == f"/workspace/{repository.name}/sub"


def test_external_worktree_rejected(repository: Path, tmp_path: Path) -> None:
    tree = tmp_path / "outside"
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "add", "-b", "outside", str(tree)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(DevboxError, match="inside the primary"):
        Project.discover(tree)


@pytest.mark.parametrize(
    "image",
    [
        'context = ".."',
        'context = "/"',
        'context = "linked"',
        'context = ".git"',
        'containerfile = "../Containerfile"',
    ],
)
def test_image_inputs_must_stay_in_project(repository: Path, image: str) -> None:
    (repository / "linked").symlink_to(repository.parent)
    (repository.parent / "Containerfile").write_text("FROM scratch\n")
    config = repository / ".devbox.toml"
    key = image.split(" =")[0]
    lines = [line for line in config.read_text().splitlines() if not line.startswith(key)]
    config.write_text("\n".join(lines).replace("[image]", f"[image]\n{image}") + "\n")
    with pytest.raises(DevboxError, match="outside .git"):
        Project.discover(repository)


@pytest.mark.parametrize(
    "suffix",
    [
        "\n[container]\ncpus = 0\n",
        "\n[container]\ncpus = true\n",
        '\n[container]\nmemory = "garbage"\n',
        "\n[environment]\nX = 3\n",
    ],
)
def test_invalid_resources_and_environment(repository: Path, suffix: str) -> None:
    with (repository / ".devbox.toml").open("a") as stream:
        stream.write(suffix)
    with pytest.raises(DevboxError):
        Project.discover(repository)


def test_builder_table_is_ignored(repository: Path) -> None:
    with (repository / ".devbox.toml").open("a") as stream:
        stream.write('\n[builder]\nmemory = "garbage"\n')
    assert not hasattr(Project.discover(repository), "builder_memory")


@pytest.mark.parametrize("path", [".pi/agent", ".pi/agent/sessions", ".pi/agent/sessions/cache"])
def test_prepared_dirs_cannot_cover_sessions(tmp_path: Path, harness: Harness, path: str) -> None:
    (tmp_path / "Containerfile").write_text("FROM ubuntu:24.04\n")
    (tmp_path / "profile.toml").write_text(
        (harness.directory / "profile.toml")
        .read_text()
        .replace('prepared_dirs = [".pi/agent/npm", ".pi/agent/git"]', f'prepared_dirs = ["{path}"]')
    )
    with pytest.raises(DevboxError, match="overlap"):
        Harness.load(tmp_path)


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(DevboxError, match="No .devbox.toml"):
        Project.discover(tmp_path)


def test_custom_profile_overrides_builtin(tmp_path: Path, harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "devbox/harnesses/pi"
    target.mkdir(parents=True)
    (target / "profile.toml").write_text(
        (harness.directory / "profile.toml").read_text().replace('command = ["devbox-pi",', 'command = ["custom-pi",')
    )
    (target / "Containerfile").write_text("ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert harnesses()["pi"].command[0] == "custom-pi"
