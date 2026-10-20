"""Validated project manifests and declarative harness profiles."""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentbox.errors import AgentboxError

ASSETS = Path(__file__).parent / "assets"
# Repository-local variables exported by Git hooks must not override --project.
LOCAL_GIT_ENV = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
        "GIT_INTERNAL_SUPER_PREFIX",
    }
)


def table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentboxError(f"{name} must be a table")
    return value


def strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise AgentboxError(f"{name} must be an array of nonempty strings")
    return tuple(value)


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AgentboxError(f"{name} must be a nonempty string")
    return value


def environment(value: Any) -> dict[str, str]:
    result = table(value, "environment")
    for key, val in result.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(val, str):
            raise AgentboxError("environment entries must be NAME = 'string'")
    return dict(result)


def relative(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] == ".git":
        raise AgentboxError(f"Expected a safe relative directory, got {value!r}")
    return str(p)


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentboxError(f"Cannot read {path}: {exc}") from exc


def git_output(directory: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key not in LOCAL_GIT_ENV},
    )
    return result.stdout.strip() if result.returncode == 0 else None


@dataclass(frozen=True)
class Project:
    config: Path
    root: Path
    cwd: Path
    containerfile: Path
    context: Path
    artifacts: tuple[str, ...]
    prepare_commands: tuple[str, ...]
    env: dict[str, str]
    cpus: int = 4
    memory: str = "8G"

    @classmethod
    def discover(cls, directory: Path) -> Project:
        cwd = directory.expanduser().resolve()
        if not cwd.is_dir():
            raise AgentboxError(f"Project directory does not exist: {cwd}")
        common = git_output(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        # Only a normal repository's .git directory identifies a primary checkout.
        # Bare repos and linked worktrees outside that checkout are not workspace mounts.
        primary = Path(common).parent if common and Path(common).name == ".git" else None
        candidates = [p / ".agentbox.toml" for p in (cwd, *cwd.parents)]
        if primary:
            candidates.append(primary / ".agentbox.toml")
        config = next((p for p in candidates if p.is_file()), None)
        if config is None:
            raise AgentboxError(f"No .agentbox.toml found for {cwd}")
        root = primary or config.parent
        if not cwd.is_relative_to(root):
            raise AgentboxError(
                "Worktrees must live inside the primary checkout (e.g. .worktrees/)"
            )
        data = load_toml(config)
        if data.get("version") != 1:
            raise AgentboxError(f"{config}: expected version = 1")
        image = table(data.get("image", {}), "image")
        workspace = table(data.get("workspace", {}), "workspace")
        container = table(data.get("container", {}), "container")
        context = (config.parent / text(image.get("context", "."), "image.context")).resolve()
        containerfile = (
            config.parent / text(image.get("containerfile", "Containerfile"), "image.containerfile")
        ).resolve()
        if not context.is_dir() or not containerfile.is_file():
            raise AgentboxError(f"Image context or Containerfile missing in {config}")
        artifacts = tuple(relative(v) for v in strings(workspace.get("artifacts", []), "artifacts"))
        for i, path in enumerate(artifacts):
            if any(
                PurePosixPath(path).is_relative_to(other)
                or PurePosixPath(other).is_relative_to(path)
                for other in artifacts[:i]
            ):
                raise AgentboxError("Artifact directories must not overlap")
        commands = strings(workspace.get("prepare_commands", []), "prepare_commands")
        if any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", c) for c in commands):
            raise AgentboxError("prepare_commands must contain executable names, not paths")
        cpus = container.get("cpus", 4)
        memory = text(container.get("memory", "8G"), "container.memory")
        if type(cpus) is not int or cpus < 1 or not re.fullmatch(r"[1-9][0-9]*[KMG]?", memory):
            raise AgentboxError("Invalid CPU count or memory size (e.g. cpus = 4, memory = '8G')")
        return cls(
            config,
            root,
            cwd,
            containerfile,
            context,
            artifacts,
            commands,
            environment(data.get("environment", {})),
            cpus,
            memory,
        )


@dataclass(frozen=True)
class Harness:
    name: str
    directory: Path
    command: tuple[str, ...]
    resume_args: tuple[str, ...]
    state_dir: str
    session_dir: str
    seed_dir: Path
    seed_files: tuple[str, ...]
    env: dict[str, str]

    @classmethod
    def load(cls, directory: Path) -> Harness:
        data = load_toml(directory / "profile.toml")
        name = text(data.get("name"), "harness.name")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise AgentboxError(f"Invalid harness name: {name!r}")
        command = strings(data.get("command"), "command")
        resume = strings(data.get("resume_args", []), "resume_args")
        if not command or (resume and not any("{session}" in arg for arg in resume)):
            raise AgentboxError("Harness needs a command and {session} in resume_args")
        if not (directory / "Containerfile").is_file():
            raise AgentboxError(f"Missing {directory / 'Containerfile'}")
        return cls(
            name,
            directory,
            command,
            resume,
            relative(text(data.get("state_dir"), "state_dir")),
            relative(text(data.get("session_dir"), "session_dir")),
            Path(text(data.get("seed_dir"), "seed_dir")).expanduser(),
            tuple(relative(v) for v in strings(data.get("seed_files", []), "seed_files")),
            environment(data.get("environment", {})),
        )


def harnesses() -> dict[str, Harness]:
    result = {}
    user = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "agentbox/harnesses"
    )
    for base in (ASSETS / "harnesses", user):
        if base.is_dir():
            for directory in sorted(base.iterdir()):
                if directory.is_dir() and (directory / "profile.toml").is_file():
                    harness = Harness.load(directory)
                    result[harness.name] = harness
    return result
