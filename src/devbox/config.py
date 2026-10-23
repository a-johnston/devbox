"""Validated project manifests and declarative harness profiles."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from devbox.errors import DevboxError

ASSETS = Path(__file__).parent / "assets"


def table(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DevboxError(f"{name} must be a table")
    return value


def strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise DevboxError(f"{name} must be an array of nonempty strings")
    return tuple(value)


def text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DevboxError(f"{name} must be a nonempty string")
    return value


def environment(value: Any) -> dict[str, str]:
    result = table(value, "environment")
    for key, val in result.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not isinstance(val, str):
            raise DevboxError("environment entries must be NAME = 'string'")
    return dict(result)


def relative(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] == ".git":
        raise DevboxError(f"Expected a safe relative directory, got {value!r}")
    return str(p)


def size(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*[KMG]?", value):
        raise DevboxError(f"{name} must be a size such as '8G'")
    return value


def overlapping(paths: tuple[str, ...]) -> bool:
    pure = [PurePosixPath(path) for path in paths]
    return any(a.is_relative_to(b) for i, a in enumerate(pure) for b in pure[:i] + pure[i + 1 :])


def load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DevboxError(f"Cannot read {path}: {exc}") from exc


def git_environment() -> dict[str, str]:
    # Variables exported by Git hooks (GIT_DIR, ...) must not override --project.
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=False,
        env=git_environment(),
    )


def git_output(directory: Path, *args: str) -> str | None:
    result = _git(directory, *args)
    return result.stdout.strip() if result.returncode == 0 else None


def git(directory: Path, *args: str) -> str:
    result = _git(directory, *args)
    if result.returncode:
        raise DevboxError(f"git {args[0]} failed in {directory}: {result.stderr.strip()}")
    return result.stdout


def init(directory: Path) -> dict[Path, bool]:
    """Write template project files at the checkout's top level; never overwrite."""
    directory = directory.expanduser().resolve()
    root = Path(git_output(directory, "rev-parse", "--show-toplevel") or directory).resolve()
    # The example tree mirrors where its files go in a project.
    example = ASSETS / "example"
    created = {}
    for template in sorted(p for p in example.rglob("*") if p.is_file()):
        path = root / template.relative_to(example)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x") as stream:
                stream.write(template.read_text())
            created[path] = True
        except FileExistsError:
            created[path] = False
    return created


@dataclass(frozen=True)
class Project:
    config: Path
    root: Path
    cwd: Path
    # The top level of cwd's worktree: what each instance clones and mounts in its place.
    checkout: Path
    containerfile: Path
    context: Path
    artifacts: tuple[str, ...]
    env: dict[str, str]
    cpus: int = 4
    memory: str = "8G"
    # Paths that fetch flags when an instance's branches change them, beyond the image inputs.
    watch: tuple[str, ...] = ()

    @classmethod
    def discover(cls, directory: Path) -> Project:
        cwd = directory.expanduser().resolve()
        if not cwd.is_dir():
            raise DevboxError(f"Project directory does not exist: {cwd}")
        common = git_output(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        # Only a normal repository's .git directory identifies a primary checkout.
        # Bare repos and linked worktrees outside that checkout are not workspace mounts.
        primary = Path(common).parent if common and Path(common).name == ".git" else None
        candidates = [p / ".devbox.toml" for p in (cwd, *cwd.parents)]
        if primary:
            candidates.append(primary / ".devbox.toml")
        config = next((p for p in candidates if p.is_file()), None)
        if config is None:
            raise DevboxError(f"No .devbox.toml found for {cwd}")
        root = primary or config.parent
        if not cwd.is_relative_to(root):
            raise DevboxError("Worktrees must live inside the primary checkout (e.g. .worktrees/)")
        data = load_toml(config)
        if data.get("version") != 1:
            raise DevboxError(f"{config}: expected version = 1")
        image = table(data.get("image", {}), "image")
        workspace = table(data.get("workspace", {}), "workspace")
        container = table(data.get("container", {}), "container")
        review = table(data.get("review", {}), "review")
        context = (config.parent / text(image.get("context", "."), "image.context")).resolve()
        containerfile = (
            config.parent / text(image.get("containerfile", "Containerfile"), "image.containerfile")
        ).resolve()
        if not context.is_dir() or not containerfile.is_file():
            raise DevboxError(f"Image context or Containerfile missing in {config}")
        # Builds read these from the host; the guest can edit the manifest that names them.
        real = root.resolve()
        for path in (context, containerfile):
            if not path.is_relative_to(real) or path.is_relative_to(real / ".git"):
                raise DevboxError(f"{path} must be inside {root} and outside .git")
        artifacts = tuple(relative(v) for v in strings(workspace.get("artifacts", []), "artifacts"))
        if overlapping(artifacts):
            raise DevboxError("Artifact directories must not overlap")
        toplevel = git_output(cwd, "rev-parse", "--show-toplevel")
        checkout = Path(toplevel).resolve() if toplevel else root
        if any(cwd.is_relative_to(checkout / artifact) for artifact in artifacts):
            raise DevboxError(f"{cwd} is hidden by a guest-local artifact directory in the guest")
        cpus = container.get("cpus", 4)
        if type(cpus) is not int or cpus < 1:
            raise DevboxError("container.cpus must be a positive integer")
        return cls(
            config,
            root,
            cwd,
            checkout,
            containerfile,
            context,
            artifacts,
            environment(data.get("environment", {})),
            cpus,
            size(container.get("memory", "8G"), "container.memory"),
            tuple(relative(v) for v in strings(review.get("watch", []), "review.watch")),
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
    prepared_dirs: tuple[str, ...] = ()
    session_glob: str = "**/*.jsonl"
    version_command: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()

    @classmethod
    def load(cls, directory: Path) -> Harness:
        data = load_toml(directory / "profile.toml")
        name = text(data.get("name"), "harness.name")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise DevboxError(f"Invalid harness name: {name!r}")
        command = strings(data.get("command"), "command")
        resume = strings(data.get("resume_args", []), "resume_args")
        if not command or (
            resume and not any("{session}" in arg or "{session_id}" in arg for arg in resume)
        ):
            raise DevboxError(
                "Harness needs a command and {session} or {session_id} in resume_args"
            )
        prepared = tuple(
            relative(v) for v in strings(data.get("prepared_dirs", []), "prepared_dirs")
        )
        session_dir = relative(text(data.get("session_dir"), "session_dir"))
        if overlapping((*prepared, session_dir)):
            raise DevboxError("Prepared directories must not overlap each other or sessions")
        if not (directory / "Containerfile").is_file():
            raise DevboxError(f"Missing {directory / 'Containerfile'}")
        return cls(
            name,
            directory,
            command,
            resume,
            relative(text(data.get("state_dir"), "state_dir")),
            session_dir,
            Path(text(data.get("seed_dir"), "seed_dir")).expanduser(),
            tuple(relative(v) for v in strings(data.get("seed_files", []), "seed_files")),
            environment(data.get("environment", {})),
            prepared,
            relative(text(data.get("session_glob", "**/*.jsonl"), "session_glob")),
            strings(data.get("version_command", []), "version_command"),
            tuple(environment({name: "" for name in strings(data.get("secrets", []), "secrets")})),
        )

    def hook(self, name: str) -> Callable[..., Any] | None:
        """An optional host-side adapter function from the profile's hooks.py."""
        path = self.directory / "hooks.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(f"devbox_hooks_{self.name}", path)
        if spec is None or spec.loader is None:
            raise DevboxError(f"Cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, name, None)


def harnesses() -> dict[str, Harness]:
    """Built-in and user profiles."""
    result = {}
    user = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "devbox/harnesses"
    )
    for base in (ASSETS / "harnesses", user):
        if base.is_dir():
            for directory in sorted(base.iterdir()):
                if directory.is_dir() and (directory / "profile.toml").is_file():
                    harness = Harness.load(directory)
                    result[harness.name] = harness
    return result
