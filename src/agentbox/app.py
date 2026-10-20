"""Build composition and lifecycle. No project- or Pi-specific dependencies here."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from agentbox.config import ASSETS, Harness, Project, git_output
from agentbox.engine import MANAGED, OWNER, Container, Engine
from agentbox.errors import AgentboxError
from agentbox.state import Session, Store, seed_config, snapshot_session

PALETTE = (24, 25, 29, 53, 60, 88, 94, 96, 130)


def mount(source: Path, target: str, *, readonly: bool = False) -> tuple[str, str]:
    if any(c in str(source) + target for c in ",\n\r"):
        raise AgentboxError("Mount paths cannot contain commas or newlines")
    return (
        "--mount",
        f"type=bind,source={source},target={target}" + (",readonly" if readonly else ""),
    )


def accent(id: str) -> int:
    return PALETTE[int(hashlib.sha256(id.encode()).hexdigest()[:8], 16) % len(PALETTE)]


def tmux_config(id: str, project: str, harness: str) -> str:
    # Do not allow project names to introduce tmux format expressions or commands.
    label = re.sub(r"[^A-Za-z0-9_. /-]", "_", f" {project} | {harness} | {id} ")
    color = accent(id)
    return (ASSETS / "runtime/tmux.conf").read_text() + (
        f"\nset -g status-style bg=colour{color},fg=colour255\n"
        f"set -g pane-active-border-style fg=colour{color}\n"
        f"set -g status-left '{label}'\n"
    )


class App:
    def __init__(self, engine: Engine | None = None, store: Store | None = None) -> None:
        self.engine = engine or Engine()
        self.store = store or Store()

    def _image_key(self, project: Project) -> str:
        return hashlib.sha256(f"{self.store.root}:{project.root}".encode()).hexdigest()[:12]

    @contextmanager
    def _project_lock(self, project: Project) -> Generator[None, None, None]:
        directory = self.store.root / "build-locks"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / self._image_key(project)).open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def build(self, project: Project, harness: Harness) -> str:
        with self._project_lock(project):
            return self._build(project, harness)

    def _build(self, project: Project, harness: Harness) -> str:
        self.engine.require_running()
        key = self._image_key(project)
        base = f"agentbox-project-{key}:latest"
        runtime = f"agentbox-runtime-{key}:latest"
        image = f"agentbox-{harness.name}-{key}:latest"
        common = (
            "--platform",
            "linux/arm64",
            "--memory",
            project.memory,
            "--cpus",
            str(project.cpus),
        )
        for tag, file, context, extra in (
            (base, project.containerfile, project.context, ()),
            (
                runtime,
                ASSETS / "runtime/Containerfile",
                ASSETS / "runtime",
                ("--build-arg", f"PROJECT_IMAGE={base}"),
            ),
            (
                image,
                harness.directory / "Containerfile",
                harness.directory,
                ("--build-arg", f"RUNTIME_IMAGE={runtime}"),
            ),
        ):
            self.engine.call(
                "build",
                *common,
                "--tag",
                tag,
                "--file",
                str(file),
                *extra,
                str(context),
                capture=False,
            )
        return image

    def new(
        self,
        project: Project,
        harness: Harness,
        *,
        no_config: bool = False,
        session: Session | None = None,
    ) -> str:
        if self.store.root.is_relative_to(project.root):
            raise AgentboxError("Keep agentbox state outside the shared project directory")
        # Hold the same lock through container creation, so another launch cannot
        # replace a shared image tag between our build and container run.
        with self._project_lock(project):
            return self._new(project, harness, no_config=no_config, session=session)

    def _new(
        self, project: Project, harness: Harness, *, no_config: bool, session: Session | None
    ) -> str:
        if session and not harness.resume_args:
            raise AgentboxError(f"{harness.name} does not support local session resumption")
        env = {**project.env, **harness.env}
        if "PATH" in env or "HOME" in env:
            raise AgentboxError("Project/harness environment must not override PATH or HOME")
        image = self._build(project, harness)
        id, directory = self.store.create()
        home = directory / "home"
        (home / harness.session_dir).mkdir(parents=True, exist_ok=True)
        if not no_config:
            imported = seed_config(harness, home)
            if imported:
                print("Seeded isolated config copy: " + ", ".join(imported), flush=True)
        command = list(harness.command)
        if session:
            target = home / ".agentbox/resume.jsonl"
            snapshot_session(
                session.path, target, root=self.store.directory(session.instance) / "home"
            )
            command.extend(
                arg.replace("{session}", "/home/agent/.agentbox/resume.jsonl")
                for arg in harness.resume_args
            )
        note = home / ".agentbox/environment.md"
        note.parent.mkdir(exist_ok=True)
        note.write_text(
            f"You are running in agentbox instance {id}, a Linux ARM64 VM, as a non-root user.\n"
            f"The whole host checkout {project.root} is writable at that same path, including .git.\n"
            "Follow the project's worktree and landing policies; this is not a branch protection boundary.\n"
            "Declared build/import outputs are guest-local and will disappear if the container is removed.\n"
            "Any previous session's processes, temporary files and OS assumptions do not carry over.\n"
            "There is no host shell bridge. Do not assume macOS tools or the host editor are available.\n"
        )
        control = directory / "control"
        config = {
            "root": str(project.root),
            "cwd": str(project.cwd),
            "artifacts": project.artifacts,
            "prepare_commands": project.prepare_commands,
            "command": command,
            "git_identity": {
                key: value
                for key in ("user.name", "user.email")
                if (value := git_output(project.cwd, "config", "--get", key))
            },
        }
        (control / "project.json").write_text(json.dumps(config, indent=2) + "\n")
        (control / "tmux.conf").write_text(tmux_config(id, project.root.name, harness.name))
        metadata = {
            "id": id,
            "project": str(project.cwd),
            "root": str(project.root),
            "harness": harness.name,
            "config": str(project.config),
            "session_dir": harness.session_dir,
            "image": image,
            "resumed_from": session.id if session else None,
        }
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        flags: list[str] = []
        for key, value in env.items():
            flags.extend(("--env", f"{key}={value}"))
        try:
            self.engine.call(
                "run",
                "--detach",
                "--init",
                "--name",
                id,
                "--cpus",
                str(project.cpus),
                "--memory",
                project.memory,
                "--cap-add",
                "SYS_ADMIN",
                "--label",
                f"{MANAGED}=1",
                "--label",
                f"{OWNER}={os.getuid()}",
                "--label",
                f"dev.agentbox.project={project.root}",
                "--label",
                f"dev.agentbox.harness={harness.name}",
                *mount(project.root, str(project.root)),
                *mount(home, "/home/agent"),
                *mount(control, "/etc/agentbox", readonly=True),
                *flags,
                image,
            )
            self._wait_ready(id)
            self.engine.exec(
                id,
                "tmux",
                "-f",
                "/etc/agentbox/tmux.conf",
                "new-session",
                "-d",
                "-s",
                "agent",
                "-c",
                str(project.cwd),
                "/opt/agentbox/launch.py",
            )
        except (AgentboxError, KeyboardInterrupt):
            # Retain state for diagnosis, but do not leak a running VM on startup failure.
            self.engine.call("stop", id, check=False)
            self.engine.call("delete", id, check=False)
            raise
        print(f"Created {id} · {harness.name} · {project.cwd}", flush=True)
        return id

    def _wait_ready(self, id: str) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if (
                self.engine.exec(id, "test", "-f", "/run/agentbox-ready", check=False).returncode
                == 0
            ):
                return
            time.sleep(0.25)
        logs = self.engine.call("logs", id, check=False)
        raise AgentboxError(f"Container initialization failed:\n{logs.stdout}{logs.stderr}")

    def attach(self, container: Container) -> None:
        if container.state != "running":
            raise AgentboxError(
                f"{container.id} is stopped; resume a saved conversation in a new container"
            )
        if self.engine.exec(
            container.id, "tmux", "has-session", "-t", "agent", check=False
        ).returncode:
            raise AgentboxError(
                "Harness session has exited. Use shell to inspect, or resume saved history."
            )
        print("Detach with Ctrl-a, then d. The harness will keep running.", flush=True)
        self.engine.connect(container.id, "tmux", "attach-session", "-t", "agent")

    def shell(self, container: Container) -> None:
        if container.state != "running":
            raise AgentboxError(f"{container.id} is stopped")
        metadata = self.store.metadata(container.id)
        self.engine.connect(container.id, "/bin/bash", cwd=metadata["project"])

    def describe(self, container: Container) -> str:
        if container.state != "running":
            return container.state
        live = self.engine.exec(container.id, "tmux", "has-session", "-t", "agent", check=False)
        return "running" if live.returncode == 0 else "exited (VM running)"

    def kill_all(self) -> int:
        containers = self.engine.containers()
        for container in containers:
            self.engine.remove(container)
        return len(containers)
