import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.config import ASSETS, Harness, Project


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Git hooks export repository-local variables. Never let fixture operations
    # inherit a parent commit's index/worktree or the user's executable hooks.
    for key in os.environ:
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def isolated_devbox_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shared harness state must never touch the developer's actual credentials.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
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
