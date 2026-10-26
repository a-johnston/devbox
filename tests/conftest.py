import os
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.config import ASSETS, Harness, Project


@pytest.fixture(scope="session", autouse=True)
def isolated_git_environment() -> Iterator[None]:
    # Git hooks export repository-local variables. Never let fixture operations
    # inherit a parent commit's index/worktree or the user's executable hooks.
    with pytest.MonkeyPatch.context() as patch:
        for key in os.environ:
            if key.startswith("GIT_"):
                patch.delenv(key)
        patch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        yield


@pytest.fixture(autouse=True)
def isolated_devbox_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shared harness state must never touch the developer's actual credentials.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture(scope="session")
def repository_template(tmp_path_factory: pytest.TempPathFactory, isolated_git_environment: None) -> Path:
    root = tmp_path_factory.mktemp("repository-template")
    subprocess.run(["git", "init", "--template=", "-b", "main", str(root)], check=True, capture_output=True)
    # Empty template omits sample hooks; retain the directories used by tests.
    (root / ".git/info").mkdir()
    (root / ".git/hooks").mkdir()
    (root / ".devbox").mkdir()
    (root / ".devbox/Containerfile").write_text("FROM ubuntu:24.04\n")
    (root / ".devbox.toml").write_text("""version = 1
[image]
containerfile = ".devbox/Containerfile"
context = ".devbox"
""")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "Initial",
        ],
        check=True,
        capture_output=True,
    )
    return root


@pytest.fixture
def repository(tmp_path: Path, repository_template: Path) -> Path:
    # Copy, never share/hardlink: tests mutate refs, config, index and worktrees.
    # Only the initial git init/add/commit is amortized across the session.
    return Path(shutil.copytree(repository_template, tmp_path / "project with spaces"))


@pytest.fixture
def project(repository: Path) -> Project:
    return Project.discover(repository)


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    # Never run the host's harness CLI from unit tests.
    return replace(
        Harness.load(ASSETS / "harnesses/pi"),
        seed_dir=tmp_path / "host-config",
        version_command=(),
    )
