"""A deliberately small adapter to Apple's container CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from agentbox.errors import AgentboxError

MANAGED = "dev.agentbox.managed"
OWNER = "dev.agentbox.owner"


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
