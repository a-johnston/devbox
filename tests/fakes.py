"""Application-level engine fake: records requests, never translates backend CLI arguments."""

import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from devbox.engine import Container, Engine, Image, RunSpec, backend_name


@dataclass(frozen=True)
class BuildRequest:
    tag: str
    file: Path
    context: Path
    args: dict[str, str]
    platform: str
    cpus: int
    labels: dict[str, str]


@dataclass(frozen=True)
class ExecRequest:
    id: str
    args: tuple[str, ...]
    user: str
    check: bool
    env: dict[str, str] | None


class FakeEngine(Engine):
    name = backend_name()
    isolation = "VM"

    def __init__(self) -> None:
        self.builds: list[BuildRequest] = []
        self.runs: list[RunSpec] = []
        self.execs: list[ExecRequest] = []
        self.connections: list[tuple[str, tuple[str, ...], str | None]] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.rows: list[Container] = []
        self.built_images: set[str] = set()
        self.image_checks: list[str] = []
        self.build_scopes = 0
        self.building_active = False

    def call(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("Application tests must not invoke backend CLI plumbing")

    def require_running(self) -> None:
        pass

    def image_exists(self, reference: str) -> bool:
        self.image_checks.append(reference)
        return reference in self.built_images

    @contextmanager
    def building(self) -> Generator[None]:
        self.build_scopes += 1
        self.building_active = True
        try:
            yield
        finally:
            self.building_active = False

    def build(
        self,
        tag: str,
        file: Path,
        context: Path,
        args: dict[str, str],
        *,
        platform: str,
        cpus: int,
        labels: dict[str, str],
    ) -> None:
        assert self.building_active
        self.builds.append(BuildRequest(tag, file, context, args, platform, cpus, labels))
        self.built_images.add(tag)

    def images(self) -> list[Image]:
        return []

    def container_images(self) -> set[str]:
        return set()

    def remove_image(self, reference: str) -> None:
        raise AssertionError("No image inventory configured")

    def containers(self) -> list[Container]:
        return self.rows

    def container_ids(self) -> set[str]:
        return {row.id for row in self.rows}

    def run(self, spec: RunSpec) -> None:
        self.runs.append(spec)

    def exec(
        self,
        id: str,
        *args: str,
        user: str = "agent",
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.execs.append(ExecRequest(id, args, user, check, env))
        return subprocess.CompletedProcess(args, 0, "", "")

    def connect(self, id: str, *args: str, cwd: str | None = None) -> None:
        self.connections.append((id, args, cwd))

    def stop(self, id: str) -> None:
        self.stopped.append(id)

    def remove(self, id: str) -> None:
        self.removed.append(id)
