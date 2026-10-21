"""Host-backed per-instance state. Container deletion never deletes this tree."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from agentbox.config import Harness
from agentbox.errors import AgentboxError


@dataclass(frozen=True)
class Session:
    id: str
    instance: str
    harness: str
    project: str
    path: Path


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            (
                root
                or Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
                / "agentbox"
            )
            .expanduser()
            .resolve()
        )

    @property
    def instances(self) -> Path:
        return self.root / "instances"

    def create(self) -> tuple[str, Path]:
        id = "agentbox-" + secrets.token_hex(4)
        directory = self.instances / id
        directory.mkdir(parents=True, mode=0o700)
        (directory / "home").mkdir(mode=0o700)
        (directory / "control").mkdir(mode=0o700)
        return id, directory

    def directory(self, id: str) -> Path:
        if not id.startswith("agentbox-") or "/" in id or ".." in id:
            raise AgentboxError("Invalid instance ID")
        directory = self.instances / id
        if not (directory / "metadata.json").is_file():
            raise AgentboxError(f"No saved state for {id}")
        return directory

    def metadata(self, id: str) -> dict[str, Any]:
        try:
            return json.loads((self.directory(id) / "metadata.json").read_text())
        except (OSError, ValueError) as exc:
            raise AgentboxError(f"Cannot read saved state for {id}") from exc

    def sessions(self) -> list[Session]:
        if not self.instances.exists():
            return []
        sessions = []
        for directory in sorted(self.instances.iterdir()):
            if not (directory / "metadata.json").is_file():
                continue
            metadata = self.metadata(directory.name)
            home = directory / "home"
            base = home / metadata["session_dir"]
            if not base.resolve().is_relative_to(home):
                continue
            for path in sorted(base.rglob("*.jsonl")):
                if path.is_symlink() or not path.is_file():
                    continue
                # Display metadata, never prompt text or credentials from the transcript.
                try:
                    with session_file(path, home) as stream:
                        header = json.loads(stream.readline(65536))
                    session_id = str(header.get("id", path.stem))
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id):
                        continue
                except (AgentboxError, OSError, ValueError, AttributeError):
                    continue
                sessions.append(
                    Session(
                        f"{directory.name}/{session_id}",
                        directory.name,
                        metadata["harness"],
                        metadata["project"],
                        path,
                    )
                )
        return sessions


def package_sources(harness: Harness, *, no_config: bool = False) -> tuple[str, ...]:
    """Only portable package declarations enter images, never private settings/auth."""
    source = harness.seed_dir / "settings.json"
    if (
        no_config
        or harness.name != "pi"
        or "settings.json" not in harness.seed_files
        or source.is_symlink()
        or not source.is_file()
    ):
        return ()
    try:
        data = json.loads(source.read_text())
        result = set()
        packages = data.get("packages", [])
        if not isinstance(packages, list):
            raise ValueError("packages must be an array")
        for package in packages:
            value = package if isinstance(package, str) else package.get("source", "")
            if not value.startswith(("npm:", "git:", "https://", "http://")):
                continue
            # Declarations are visible in image layers and build logs. Reject
            # URL credentials/query strings rather than accidentally baking tokens.
            url = value.removeprefix("git:").removeprefix("npm:")
            if "://" in url:
                parsed = urlsplit(url)
                if parsed.username or parsed.password or parsed.query:
                    raise AgentboxError(
                        "Prepared package URLs must not contain credentials or query strings"
                    )
            if any(c in value for c in "\n\r\x00") or (
                value.startswith("npm:")
                and not re.fullmatch(
                    r"npm:(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_][A-Za-z0-9_.-]*(?:@[^:/\s]+)?", value
                )
            ):
                raise AgentboxError("Invalid portable package declaration")
            result.add(value)
        return tuple(sorted(result))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AgentboxError("Pi settings.json is invalid; fix it or use --no-config") from exc


def seed_config(harness: Harness, home: Path) -> list[str]:
    """Copy a small allowlist, never mount the host harness home or its package tree."""
    destination = home / harness.state_dir
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    messages = []
    for name in harness.seed_files:
        source = harness.seed_dir / name
        if source.is_symlink() or not source.is_file():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o600)
        messages.append(name)
    settings = destination / "settings.json"
    if harness.name == "pi" and settings.is_file():
        try:
            data = json.loads(settings.read_text())
            # These refer to host resources or could redirect durable session storage.
            for key in (
                "sessionDir",
                "shellPath",
                "shellCommandPrefix",
                "npmCommand",
                "externalEditor",
                "extensions",
                "skills",
                "prompts",
                "themes",
            ):
                if key in data:
                    del data[key]
                    messages.append(f"omitted host-specific setting {key}")
            packages = data.get("packages", [])
            portable = []
            for package in packages:
                source = package if isinstance(package, str) else package.get("source", "")
                if source.startswith(("npm:", "git:", "https://", "http://")):
                    portable.append(package)
                else:
                    messages.append(f"omitted local package {source}")
            data["packages"] = portable
            settings.write_text(json.dumps(data, indent=2) + "\n")
        except (ValueError, AttributeError, TypeError) as exc:
            raise AgentboxError("Pi settings.json is invalid; fix it or use --no-config") from exc
    return messages


@contextmanager
def session_file(source: Path, root: Path) -> Generator[BinaryIO, None, None]:
    """Read agent-writable state without following symlinks into unmounted host files.

    Open each component relative to a directory descriptor, so even a running
    agent swapping symlinks during resume cannot redirect a host-side read.
    """
    try:
        parts = source.relative_to(root).parts
    except ValueError as exc:
        raise AgentboxError("Session path is outside its saved home") from exc
    if not parts or ".." in parts:
        raise AgentboxError("Session path is outside its saved home")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(file).st_mode):
            os.close(file)
            raise AgentboxError("Session must be a regular file")
        with os.fdopen(file, "rb") as stream:
            yield stream
    finally:
        os.close(directory)


def snapshot_session(source: Path, target: Path, *, root: Path | None = None) -> None:
    """Snapshot complete JSONL records; a live source may currently be appending a record."""
    with session_file(source, root or source.parent) as stream:
        data = stream.read()
    end = data.rfind(b"\n")
    if end < 0:
        raise AgentboxError("Session has no complete JSONL records")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data[: end + 1])
    target.chmod(0o600)
