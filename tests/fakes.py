"""Application-level fakes: record requests, never translate backend CLI arguments or run Git."""

import subprocess
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from devbox.engine import Container, Engine, Image, RunSpec, backend_name
from devbox.errors import DevboxError
from devbox.repository import Delivery, Fetched, Landed, Repository, Started

BASE = "0123456789abcdef" * 2 + "01234567"
STARTED = Started("main", BASE, dirty=False)


class FakeRepository(Repository):
    """In-memory host Git: records what App asks for and answers with the configured results.
    Instances start from an empty stand-in clone. Use Git itself to test Git behavior."""

    def __init__(self, started: Started = STARTED, identity: dict[str, str] | None = None) -> None:
        self.started = started
        self.author = identity or {}
        self.fetched: list[Fetched] = []
        # What land returns, or raises.
        self.landed: Landed | DevboxError = Landed("main", "fetched", "rebased")
        self.delivery = Delivery("", {})
        self.calls: list[tuple[object, ...]] = []

    def start(self, checkout: Path, target: Path) -> Started:
        (target / ".git").mkdir(parents=True)
        return self.started

    def clone_instance(self, source: Path, target: Path) -> str:
        (target / ".git").mkdir(parents=True)
        return "copied"

    def identity(self, directory: Path) -> dict[str, str]:
        return self.author

    def fetch(
        self, repository: Path, clone: Path, namespace: str, watched: Sequence[str], *, branch_prefix: str
    ) -> list[Fetched]:
        self.calls.append(("fetch", repository, clone, namespace, list(watched), branch_prefix))
        return list(self.fetched)

    def show_diff(self, repository: Path, ref: str, *options: str) -> None:
        self.calls.append(("show_diff", repository, ref, *options))

    def land(self, repository: Path, fetched: Fetched) -> Landed:
        self.calls.append(("land", repository, fetched))
        if isinstance(self.landed, DevboxError):
            raise self.landed
        return self.landed

    def bundle(self, repository: Path, branches: Sequence[str], base: str) -> Delivery:
        self.calls.append(("bundle", repository, list(branches), base))
        return self.delivery


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
    check: bool
    env: dict[str, str] | None
    input: str | None = None


class FakeEngine(Engine):
    name = backend_name()

    def __init__(self) -> None:
        self.builds: list[BuildRequest] = []
        self.runs: list[RunSpec] = []
        self.execs: list[ExecRequest] = []
        self.connections: list[tuple[str, tuple[str, ...], str | None]] = []
        self.removed: list[str] = []
        self.rows: list[Container] = []
        self.built_images: set[str] = set()
        self.image_checks: list[str] = []
        self.build_scopes = 0
        self.building_active = False
        # What every exec prints, as a guest script would.
        self.stdout = ""

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
        check: bool = True,
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.execs.append(ExecRequest(id, args, check, env, input))
        return subprocess.CompletedProcess(args, 0, self.stdout, "")

    def pipe(self, id: str, *args: str, **kwargs: object) -> None:
        raise AssertionError("No guest configured for streamed exec")

    def connect(self, id: str, *args: str, cwd: str | None = None) -> None:
        self.connections.append((id, args, cwd))

    def remove(self, id: str) -> None:
        self.removed.append(id)
