"""A deliberately small adapter to Apple's container CLI."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentbox.errors import AgentboxError

MANAGED = "dev.agentbox.managed"
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
        raise AgentboxError(f"Unsupported host architecture: {machine!r}")
    return PLATFORMS[machine.lower()]


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

    def build(
        self,
        tag: str,
        file: Path,
        context: Path,
        args: dict[str, str],
        *,
        platform: str,
        memory: str,
        cpus: int,
    ) -> None:
        """Starts the builder if needed; the resources apply to (and may restart) it."""
        flags = [flag for key, value in args.items() for flag in ("--build-arg", f"{key}={value}")]
        self.call(
            "build",
            "--platform",
            platform,
            "--memory",
            memory,
            "--cpus",
            str(cpus),
            "--tag",
            tag,
            "--file",
            str(file),
            *flags,
            str(context),
            capture=False,
        )

    def builder_running(self) -> bool:
        result = self.call("builder", "status", "--format", "json", check=False)
        try:
            return any(row["status"]["state"] == "running" for row in json.loads(result.stdout))
        except (ValueError, KeyError, TypeError):
            return False

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
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentboxError(
                "Unexpected container JSON; agentbox requires container 1.5+"
            ) from exc

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
            raise AgentboxError("container executable not found")
        # Do not leak the outer tmux socket/name into the inner client.
        env = dict(os.environ)
        env.pop("TMUX", None)
        flags = ["--workdir", cwd] if cwd else []
        os.execvpe(
            executable, [executable, "exec", "-it", "--user", "agent", *flags, id, *args], env
        )

    def remove(self, id: str) -> None:
        """Delete a container and the artifact volumes named after it."""
        self.call("stop", id, check=False)
        self.call("delete", id)
        for volume in self.volumes(id):
            self.call("volume", "delete", volume)

    def volumes(self, id: str) -> list[str]:
        result = self.call("volume", "list", "--format", "json")
        try:
            names = [row["id"] for row in json.loads(result.stdout)]
        except (ValueError, KeyError, TypeError) as exc:
            raise AgentboxError("Unexpected volume JSON") from exc
        return [name for name in names if name.startswith(f"{id}-")]
