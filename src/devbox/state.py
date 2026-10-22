"""Host-backed per-instance state. Container deletion never deletes this tree."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from devbox.config import Harness, harnesses
from devbox.errors import DevboxError


@dataclass(frozen=True)
class Session:
    id: str
    instance: str
    harness: str
    project: str
    path: Path
    title: str | None = None


@dataclass(frozen=True)
class Instance:
    id: str
    size: int


class Store:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            (
                root
                or Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
                / "devbox"
            )
            .expanduser()
            .resolve()
        )

    @property
    def instances(self) -> Path:
        return self.root / "instances"

    def create(self) -> tuple[str, Path]:
        id = "devbox-" + secrets.token_hex(4)
        directory = self.instances / id
        directory.mkdir(parents=True, mode=0o700)
        (directory / "home").mkdir(mode=0o700)
        return id, directory

    def directory(self, id: str) -> Path:
        directory = self.instances / id
        if not (directory / "metadata.json").is_file():
            raise DevboxError(f"No saved state for {id}")
        return directory

    def metadata(self, id: str) -> dict[str, Any]:
        try:
            return json.loads((self.directory(id) / "metadata.json").read_text())
        except (OSError, ValueError) as exc:
            raise DevboxError(f"Cannot read saved state for {id}") from exc

    def _instance_directories(self) -> list[Path]:
        if self.instances.is_symlink():
            raise DevboxError(
                f"Refusing to clean a symlinked instances directory: {self.instances}"
            )
        if not self.instances.exists():
            return []
        return [
            directory
            for directory in sorted(self.instances.iterdir())
            if not directory.is_symlink()
            and directory.is_dir()
            and not (directory / "metadata.json").is_symlink()
            and (
                (directory / "metadata.json").is_file()
                # A failed launch may leave a home before metadata has been written.
                or ((directory / "home").is_dir() and not (directory / "home").is_symlink())
            )
        ]

    def orphaned(self, container_ids: set[str]) -> list[Instance]:
        return [
            Instance(directory.name, disk_usage(directory))
            for directory in self._instance_directories()
            if directory.name not in container_ids
        ]

    def image_references(self, excluding: set[str]) -> set[str]:
        """Retained instance homes protect their images, even without a live container."""
        references = set()
        for directory in self._instance_directories():
            if directory.name in excluding:
                continue
            metadata = self.metadata(directory.name)
            reference = metadata.get("image") if isinstance(metadata, dict) else None
            if not isinstance(reference, str) or not reference or reference.startswith("-"):
                raise DevboxError(
                    f"Cannot determine saved image for {directory.name}; refusing image cleanup"
                )
            references.add(reference)
        return references

    def remove_instance(self, id: str) -> None:
        # Only real, immediate instance directories qualify; never trust metadata paths.
        if id not in {directory.name for directory in self._instance_directories()}:
            raise DevboxError(f"Not a removable instance: {id}")
        shutil.rmtree(self.instances / id)

    def sessions(self) -> list[Session]:
        if not self.instances.exists():
            return []
        sessions = []
        hooks = title_hooks()
        for directory in sorted(self.instances.iterdir()):
            if not (directory / "metadata.json").is_file():
                continue
            metadata = self.metadata(directory.name)
            home = directory / "home"
            glob = metadata.get("session_glob", "**/*.jsonl")
            for path in sorted((home / metadata["session_dir"]).glob(glob)):
                # Only adapters' explicit title metadata may be displayed, never prompt text.
                try:
                    header = json.loads(read_session(path, home, header=True))
                    session_id = str(header.get("id", path.stem))
                    if not re.fullmatch(r"[A-Za-z0-9_.-]+", session_id):
                        continue
                except (DevboxError, OSError, ValueError, AttributeError):
                    continue
                title = None
                hook = hooks.get(metadata["harness"])
                if hook:
                    # A title is optional; keep the conversation resumable if it cannot be read.
                    with suppress(Exception):
                        title = clean_title(hook(session_records(path, home)))
                sessions.append(
                    Session(
                        f"{directory.name}/{session_id}",
                        directory.name,
                        metadata["harness"],
                        metadata["project"],
                        path,
                        title,
                    )
                )
        return sessions


def disk_usage(directory: Path) -> int:
    """Approximate allocated bytes, without following prepared-directory or user symlinks."""
    total = 0
    seen = set()
    pending = [directory]
    while pending:
        path = pending.pop()
        info = path.lstat()
        inode = (info.st_dev, info.st_ino)
        if inode in seen:
            continue
        seen.add(inode)
        total += info.st_blocks * 512
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
    return total


def run_hook(harness: Harness, name: str, *args: Any) -> Any:
    hook = harness.hook(name)
    try:
        return hook(*args) if hook else None
    except ValueError as exc:
        raise DevboxError(str(exc)) from exc


def build_files(harness: Harness, *, no_config: bool = False) -> dict[str, str]:
    """Extra harness build-context files derived from host config, e.g. package lists."""
    if no_config:
        return {}
    files = run_hook(harness, "build_files", harness.seed_dir) or {}
    if any("/" in name or name.startswith(".") for name in files):
        raise DevboxError(f"{harness.name} hooks returned an invalid build file name")
    return files


def host_version(harness: Harness) -> str | None:
    """The harness version installed on the host, so the guest can match it."""
    if not harness.version_command:
        return None
    try:
        result = subprocess.run(
            harness.version_command, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Only a plain semantic version may reach a build argument and package spec.
    found = re.search(r"\b\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?\b", result.stdout)
    return found.group() if found and result.returncode == 0 else None


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
    return messages + (run_hook(harness, "adapt_seed", destination) or [])


def title_hooks() -> dict[str, Callable[..., Any]]:
    """Load each harness's title adapter once; a broken profile or hook only loses titles."""
    hooks = {}
    for name, harness in harnesses(strict=False).items():
        with suppress(Exception):
            if hook := harness.hook("session_title"):
                hooks[name] = hook
    return hooks


def clean_title(value: Any) -> str | None:
    """Keep agent-written titles on one terminal-safe, reasonably short line."""
    if not isinstance(value, str):
        return None
    title = " ".join("".join(c for c in value if c.isprintable() or c.isspace()).split())
    return (title[:97] + "..." if len(title) > 100 else title) or None


def _open_session(path: Path, home: Path) -> BinaryIO:
    """Open agent-writable state without following symlinks out of its saved home."""
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(home.resolve()):
        raise DevboxError(f"Session must be a regular file inside {home}")
    return path.open("rb")


def read_session(path: Path, home: Path, *, header: bool = False) -> bytes:
    with _open_session(path, home) as stream:
        return stream.readline(65536) if header else stream.read()


def session_records(path: Path, home: Path) -> Iterator[dict[str, Any]]:
    """Stream complete JSON objects for title adapters with bounded per-record memory."""
    with _open_session(path, home) as stream:
        while line := stream.readline(65536):
            if not line.endswith(b"\n"):
                # Skip oversized records and the partial tail of a live transcript.
                while line and not line.endswith(b"\n"):
                    line = stream.readline(65536)
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(record, dict):
                yield record


def snapshot_session(source: Path, target: Path, *, root: Path | None = None) -> None:
    """Snapshot complete JSONL records; a live source may currently be appending a record."""
    data = read_session(source, root or source.parent)
    end = data.rfind(b"\n")
    if end < 0:
        raise DevboxError("Session has no complete JSONL records")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data[: end + 1])
    target.chmod(0o600)
