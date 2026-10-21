import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agentbox.config import ASSETS, Harness, Project


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
def isolated_builder_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentbox.engine.BUILDER_LOCK", tmp_path / "builder.lock")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "project with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    (root / ".agentbox").mkdir()
    (root / ".agentbox/Containerfile").write_text("FROM ubuntu:24.04\n")
    (root / ".agentbox.toml").write_text("""version = 1
[image]
containerfile = ".agentbox/Containerfile"
context = ".agentbox"
[workspace]
artifacts = [".godot", "tests/bin", "tests/obj"]
prepare_commands = ["git", "dotnet"]
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
    return replace(Harness.load(ASSETS / "harnesses/pi"), seed_dir=tmp_path / "host-config")
