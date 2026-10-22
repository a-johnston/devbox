"""The small container-engine interface App uses, and the CLI plumbing its backends share."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from devbox.errors import DevboxError

MANAGED = "dev.devbox.managed"
IMAGE_STORE = "dev.devbox.image.store"
IMAGE_SCHEMA = "dev.devbox.image.schema"
IMAGE_KIND = "dev.devbox.image.kind"
PLATFORMS = {
    "arm64": "linux/arm64",
    "aarch64": "linux/arm64",
    "x86_64": "linux/amd64",
    "amd64": "linux/amd64",
}


def host_platform() -> str:
    """Images build natively; there is no cross-architecture emulation."""
    machine = platform.machine()
    if machine.lower() not in PLATFORMS:
        raise DevboxError(f"Unsupported host architecture: {machine!r}")
    return PLATFORMS[machine.lower()]


def mount(source: Path | str, target: str, *, type: str = "bind") -> tuple[str, str]:
    if any(c in str(source) + target for c in ",\n\r"):
        raise DevboxError("Mount paths cannot contain commas or newlines")
    return "--mount", f"type={type},source={source},target={target}"


@dataclass(frozen=True)
class Container:
    id: str
    state: str
    project: str
    harness: str
    created: str


def image_identity(reference: str) -> str:
    """Match local build tags across the engines' default-registry qualification."""
    return (
        reference.removeprefix("docker.io/library/")
        .removeprefix("localhost/")
        .removeprefix("sha256:")
    )


def image_strings(values: object) -> tuple[str, ...]:
    """Inventory must fail closed, never silently drop an unrecognizable reference."""
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value or value.startswith("-") for value in values
    ):
        raise ValueError("Expected image references")
    return tuple(values)


def image_labels(config: object) -> dict[str, str]:
    # OCI config and Labels are optional; other unexpected types are not absence.
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("Expected OCI image config")
    labels = config.get("Labels")
    if labels is None:
        return {}
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise ValueError("Expected image labels")
    return labels


@dataclass(frozen=True)
class Image:
    reference: str
    id: str
    labels: dict[str, str]
    aliases: tuple[str, ...] = ()

    @property
    def identities(self) -> set[str]:
        return {image_identity(value) for value in (self.reference, self.id, *self.aliases)}


@dataclass(frozen=True)
class RunSpec:
    name: str
    image: str
    cpus: int
    memory: str
    labels: dict[str, str]
    # (host source, guest target) and (volume name, guest target), in mount order.
    binds: tuple[tuple[str, str], ...]
    volumes: tuple[tuple[str, str], ...]
    env: dict[str, str]


class Engine(ABC):
    name: str
    executable: str
    install_hint: str
    # How the guest is isolated from the host, as told to the agent.
    isolation: str
    # Backend-specific `run` flags, placed before the shared ones.
    run_options: tuple[str, ...] = ()

    def call(
        self,
        *args: str,
        capture: bool = True,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.executable, *args],
                text=True,
                capture_output=capture,
                check=False,
                env={**os.environ, **env} if env else None,
            )
        except FileNotFoundError as exc:
            raise DevboxError(self.install_hint) from exc
        if check and result.returncode:
            detail = (result.stderr or "").strip() if capture else "See output above."
            raise DevboxError(f"{self.executable} {args[0]} failed: {detail}")
        return result

    @abstractmethod
    def require_running(self) -> None: ...

    @abstractmethod
    def image_exists(self, reference: str) -> bool: ...

    @abstractmethod
    def building(self) -> AbstractContextManager[None]:
        """Wraps a sequence of builds, for engines that need a builder service."""

    @abstractmethod
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
    ) -> None: ...

    @abstractmethod
    def images(self) -> list[Image]:
        """Images, including dangling ones, with OCI config ownership labels."""

    @abstractmethod
    def container_images(self) -> set[str]:
        """Image references and immutable IDs used by ALL running or stopped containers."""

    @abstractmethod
    def remove_image(self, reference: str) -> None:
        """Delete one explicit reference without force or global pruning."""

    @abstractmethod
    def container_ids(self) -> set[str]:
        """All containers, including stopped and differently labelled ones, protect saved state."""

    @abstractmethod
    def containers(self) -> list[Container]:
        """Only containers carrying our ownership label."""

    @abstractmethod
    def remove(self, id: str) -> None:
        """Delete a container and only its own artifact volumes."""

    def run(self, spec: RunSpec) -> None:
        flags = [
            *(flag for key, value in spec.labels.items() for flag in ("--label", f"{key}={value}")),
            *(flag for source, target in spec.binds for flag in mount(source, target)),
            *(flag for key, value in spec.env.items() for flag in ("--env", f"{key}={value}")),
            *(
                flag
                for source, target in spec.volumes
                for flag in mount(source, target, type="volume")
            ),
        ]
        self.call(
            "run",
            "--detach",
            "--init",
            "--name",
            spec.name,
            *self.run_options,
            "--cpus",
            str(spec.cpus),
            "--memory",
            spec.memory,
            *flags,
            spec.image,
        )

    def exec(
        self,
        id: str,
        *args: str,
        user: str = "agent",
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Named variables are inherited from this process, keeping values out of arguments."""
        flags = [flag for name in env or {} for flag in ("--env", name)]
        return self.call("exec", "--user", user, *flags, id, *args, check=check, env=env)

    def connect(self, id: str, *args: str, cwd: str | None = None) -> None:
        """Replace the wrapper; detaching the tmux client returns to the host shell."""
        executable = shutil.which(self.executable)
        if executable is None:
            raise DevboxError(self.install_hint)
        # Do not leak the outer tmux socket/name into the inner client.
        env = dict(os.environ)
        env.pop("TMUX", None)
        flags = ["--workdir", cwd] if cwd else []
        os.execvpe(
            executable, [executable, "exec", "-it", "--user", "agent", *flags, id, *args], env
        )

    def stop(self, id: str) -> None:
        self.call("stop", id)


def backend_name() -> str:
    """The host OS determines the backend and its saved-state namespace."""
    defaults = {"darwin": "apple", "linux": "podman"}
    if sys.platform not in defaults:
        raise DevboxError(f"No container engine for {sys.platform}")
    return defaults[sys.platform]


def select_engine() -> Engine:
    from devbox.apple import AppleEngine
    from devbox.podman import PodmanEngine

    engines: dict[str, type[Engine]] = {"apple": AppleEngine, "podman": PodmanEngine}
    return engines[backend_name()]()
