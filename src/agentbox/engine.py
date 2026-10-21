"""A deliberately small adapter to Apple's container CLI."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbox.errors import AgentboxError

MANAGED = "dev.agentbox.managed"
OWNER = "dev.agentbox.owner"
# Independent of project and --state-dir: Apple's builder is shared by this user.
BUILDER_LOCK = Path.home() / ".cache/agentbox/apple-builder.lock"


@dataclass(frozen=True)
class Container:
    id: str
    state: str
    project: str
    harness: str
    created: str


class Engine:
    def __init__(self, executable: str = "container") -> None:
        self.executable = executable

    def call(
        self, *args: str, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.executable, *args], text=True, capture_output=capture, check=False
            )
        except FileNotFoundError as exc:
            raise AgentboxError(
                "Apple's container CLI is required: https://github.com/apple/container"
            ) from exc
        if check and result.returncode:
            detail = (result.stderr or "").strip() if capture else "See output above."
            raise AgentboxError(f"container {args[0]} failed: {detail}")
        return result

    def require_running(self) -> None:
        result = self.call("system", "status", check=False)
        if result.returncode or "not running" in result.stdout:
            raise AgentboxError("Start Apple's container service first: container system start")

    def image_exists(self, reference: str) -> bool:
        result = self.call("image", "inspect", reference, check=False)
        if result.returncode == 0:
            return True
        if "image not found:" in result.stderr.lower():
            return False
        raise AgentboxError(f"Cannot check prepared image: {result.stderr.strip()}")

    @contextmanager
    def builder(self, *, memory: str, cpus: int, keep: bool = False) -> Generator[None, None, None]:
        """Lease the shared builder; only stop one this operation started.

        All Agentbox projects/stores coordinate here. Raw `container build`
        invocations do not participate; they must not overlap an ephemeral lease.
        """
        BUILDER_LOCK.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with BUILDER_LOCK.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.call("list", "--all", "--format", "json")
            try:
                rows = json.loads(result.stdout)
                running = False
                for row in rows:
                    if row["id"] != "buildkit":
                        continue
                    if (
                        row["configuration"]
                        .get("labels", {})
                        .get("com.apple.container.resource.role")
                        != "builder"
                    ):
                        raise AgentboxError(
                            "The existing buildkit container is not Apple's builder"
                        )
                    state = row["status"]["state"]
                    if state not in ("running", "stopped"):
                        raise AgentboxError(f"Builder is {state}; retry when its state is stable")
                    running = state == "running"
            except (ValueError, KeyError, TypeError) as exc:
                raise AgentboxError(
                    "Cannot determine builder ownership from container JSON"
                ) from exc
            started = False
            try:
                if not running:
                    # Set before the call: cancellation may arrive after VM startup.
                    started = True
                    self.call("builder", "start", "--memory", memory, "--cpus", str(cpus))
                yield
            finally:
                if started and not keep:
                    try:
                        stopped = self.call("builder", "stop", check=False)
                        if stopped.returncode:
                            raise AgentboxError("builder stop failed")
                    except (AgentboxError, OSError):
                        print(
                            "agentbox: builder cleanup failed; run container builder stop",
                            file=sys.stderr,
                        )

    def containers(self) -> list[Container]:
        result = self.call("list", "--all", "--format", "json")
        try:
            rows: list[dict[str, Any]] = json.loads(result.stdout)
            return [
                Container(
                    row["id"],
                    row["status"]["state"],
                    row["configuration"]["labels"].get("dev.agentbox.project", "?"),
                    row["configuration"]["labels"].get("dev.agentbox.harness", "?"),
                    row["configuration"].get("creationDate", "?"),
                )
                for row in rows
                if row["configuration"].get("labels", {}).get(MANAGED) == "1"
                and row["configuration"]["labels"].get(OWNER) == str(os.getuid())
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentboxError(
                "Unexpected container JSON; agentbox requires container 1.5+"
            ) from exc

    def exec(
        self, id: str, *args: str, interactive: bool = False, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        flags = ("-it",) if interactive else ()
        return self.call(
            "exec", *flags, "--user", "agent", id, *args, capture=not interactive, check=check
        )

    def connect(self, id: str, *args: str, cwd: str | None = None) -> None:
        """Replace the wrapper; detaching the tmux client returns to the host shell."""
        executable = shutil.which(self.executable)
        if executable is None:
            raise AgentboxError("container executable not found")
        # Do not leak the outer tmux socket/name into the inner client.
        env = dict(os.environ)
        env.pop("TMUX", None)
        flags = ["--workdir", cwd] if cwd else []
        os.execvpe(
            executable, [executable, "exec", "-it", "--user", "agent", *flags, id, *args], env
        )

    def remove(self, container: Container) -> None:
        if container.state != "stopped":
            self.call("stop", container.id)
        self.call("delete", container.id)
