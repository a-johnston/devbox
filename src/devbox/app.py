"""Build composition and lifecycle. No project- or Pi-specific dependencies here."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath

from devbox.config import ASSETS, Harness, Project, git_output, relative
from devbox.engine import MANAGED, Container, Engine, host_platform
from devbox.errors import DevboxError
from devbox.state import (
    Session,
    Store,
    build_files,
    host_version,
    run_hook,
    seed_config,
    snapshot_session,
)

PREPARED = PurePosixPath("/opt/devbox/prepared")
PALETTE = (24, 25, 29, 53, 60, 88, 94, 96, 130)
# Terminal capabilities the guest cannot detect itself; manifests and profiles override them.
INHERITED = ("COLORTERM",)


def mount(source: Path | str, target: str, *, type: str = "bind") -> tuple[str, str]:
    if any(c in str(source) + target for c in ",\n\r"):
        raise DevboxError("Mount paths cannot contain commas or newlines")
    return "--mount", f"type={type},source={source},target={target}"


def git_identity(directory: Path) -> dict[str, str]:
    """Only the author identity crosses over: no credential helpers, hooks or rewrites."""
    env = {}
    for key, name in (("user.name", "NAME"), ("user.email", "EMAIL")):
        if value := git_output(directory, "config", "--get", key):
            env[f"GIT_AUTHOR_{name}"] = env[f"GIT_COMMITTER_{name}"] = value
    return env


def environment_secrets(harness: Harness) -> dict[str, str]:
    """Declared secrets that are set on the host; nothing else crosses over."""
    return {name: value for name in harness.secrets if (value := os.environ.get(name))}


def accent(id: str) -> int:
    return PALETTE[int(hashlib.sha256(id.encode()).hexdigest()[:8], 16) % len(PALETTE)]


def tmux_config(id: str, project: str, harness: str) -> str:
    # Do not allow project names to introduce tmux format expressions or commands.
    label = re.sub(r"[^A-Za-z0-9_. /|-]", "_", f" {project} | {harness} | {id} ")
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

    def _prepared_image(
        self, project: Project, harness: Harness, files: dict[str, str], args: dict[str, str]
    ) -> str:
        # A separate namespace prevents old, unprepared images from taking the fast path.
        inputs = json.dumps(
            {"files": files, "args": args, "platform": host_platform()}, sort_keys=True
        )
        variant = hashlib.sha256(inputs.encode()).hexdigest()[:12]
        return f"devbox-{harness.name}-{self._image_key(project)}-prepared-{variant}:latest"

    def _build_args(self, harness: Harness) -> dict[str, str]:
        """Match the host's harness version; Containerfiles default to a pinned one."""
        version = host_version(harness)
        return {"HARNESS_VERSION": version} if version else {}

    def build(self, project: Project, harness: Harness, *, no_config: bool = False) -> str:
        self.engine.require_running()
        files = build_files(harness, no_config=no_config)
        return self._build(project, harness, files, self._build_args(harness))

    def _build(
        self, project: Project, harness: Harness, files: dict[str, str], args: dict[str, str]
    ) -> str:
        key = self._image_key(project)
        base = f"devbox-project-{key}:latest"
        runtime = f"devbox-runtime-{key}:latest"
        image = self._prepared_image(project, harness, files, args)
        started = time.monotonic()
        print("Preparing environment; subsequent launches reuse the prepared image.", flush=True)
        if harness.version_command:
            version = args.get("HARNESS_VERSION")
            print(
                f"Installing {harness.name} {version} to match the host."
                if version
                else f"No host {harness.name} version found; installing the pinned default.",
                flush=True,
            )
        # Stop a builder we start: it holds gigabytes of RAM and launches don't need it.
        started_builder = not self.engine.builder_running()
        try:
            with tempfile.TemporaryDirectory(prefix="devbox-build-") as temp:
                # Only harness assets and hook-selected public declarations enter this context.
                context = Path(temp) / "harness"
                shutil.copytree(
                    harness.directory, context, ignore=shutil.ignore_patterns("__pycache__")
                )
                for name, content in files.items():
                    (context / name).write_text(content)
                for tag, file, source, layer_args in (
                    (base, project.containerfile, project.context, {}),
                    (
                        runtime,
                        ASSETS / "runtime/Containerfile",
                        ASSETS / "runtime",
                        {"PROJECT_IMAGE": base},
                    ),
                    (
                        image,
                        context / "Containerfile",
                        context,
                        {
                            **args,
                            "RUNTIME_IMAGE": runtime,
                            "PREPARE_REVISION": str(time.time_ns()),
                        },
                    ),
                ):
                    self.engine.build(
                        tag,
                        file,
                        source,
                        layer_args,
                        platform=host_platform(),
                        memory=project.builder_memory,
                        cpus=project.cpus,
                    )
        finally:
            if started_builder:
                self.engine.call("builder", "stop", check=False)
        print(f"Environment prepared in {time.monotonic() - started:.1f}s", flush=True)
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
            raise DevboxError("Keep devbox state outside the shared project directory")
        if session and not harness.resume_args:
            raise DevboxError(f"{harness.name} does not support local session resumption")
        inherited = {key: os.environ[key] for key in INHERITED if key in os.environ}
        env = {**inherited, **project.env, **harness.env}
        self.engine.require_running()
        files = build_files(harness, no_config=no_config)
        args = self._build_args(harness)
        image = self._prepared_image(project, harness, files, args)
        if self.engine.image_exists(image):
            print("Using prepared image; BuildKit not needed.", flush=True)
        else:
            image = self._build(project, harness, files, args)
        # Only the harness process receives these: not the container config or saved state.
        secrets = {} if no_config else environment_secrets(harness)
        if secrets:
            print("Forwarding credentials from environment: " + ", ".join(secrets), flush=True)
        id, directory = self.store.create()
        home = directory / "home"
        (home / harness.session_dir).mkdir(parents=True, exist_ok=True)
        if not no_config:
            imported = seed_config(harness, home)
            if imported:
                print("Seeded isolated config copy: " + ", ".join(imported), flush=True)
        for prepared in harness.prepared_dirs:
            # Image-local and copy-on-write per container, never through the host mount.
            link = home / prepared
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(PREPARED / prepared)
        command = list(harness.command)
        if session:
            session_id = session.id.split("/", 1)[1]
            # Harnesses that resume by ID choose where their snapshot must live.
            target = relative(
                run_hook(harness, "resume_file", session_id, str(project.cwd))
                or ".devbox/resume.jsonl"
            )
            snapshot_session(
                session.path, home / target, root=self.store.directory(session.instance) / "home"
            )
            command.extend(
                arg.replace("{session}", f"/home/agent/{target}").replace(
                    "{session_id}", session_id
                )
                for arg in harness.resume_args
            )
        (home / ".devbox").mkdir(exist_ok=True)
        architecture = host_platform().removeprefix("linux/").upper()
        (home / ".devbox/environment.md").write_text(
            f"You are running in devbox instance {id}, a Linux {architecture} VM, as a non-root user.\n"
            f"The whole host checkout {project.root} is writable at that same path, including .git.\n"
            "Follow the project's worktree and landing policies; this is not a branch protection boundary.\n"
            "Declared build/import outputs are guest-local and will disappear if the container is removed.\n"
            "Any previous session's processes, temporary files and OS assumptions do not carry over.\n"
            "There is no host shell bridge. Do not assume host tools or the host editor are available.\n"
        )
        (home / ".devbox/tmux.conf").write_text(tmux_config(id, project.root.name, harness.name))
        metadata = {
            "id": id,
            "project": str(project.cwd),
            "root": str(project.root),
            "harness": harness.name,
            "config": str(project.config),
            "session_dir": harness.session_dir,
            "session_glob": harness.session_glob,
            "image": image,
            "resumed_from": session.id if session else None,
        }
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        flags: list[str] = []
        for key, value in {**env, **git_identity(project.cwd)}.items():
            flags.extend(("--env", f"{key}={value}"))
        artifacts = [str(project.root / artifact) for artifact in project.artifacts]
        for i, target in enumerate(artifacts):
            flags.extend(mount(f"{id}-{i}", target, type="volume"))
        started = time.monotonic()
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
                "--label",
                f"{MANAGED}=1",
                "--label",
                f"dev.devbox.project={project.root}",
                "--label",
                f"dev.devbox.harness={harness.name}",
                *mount(project.root, str(project.root)),
                *mount(home, "/home/agent"),
                *flags,
                image,
            )
            if artifacts:
                # Fresh volumes are root-owned ext4 filesystems.
                self.engine.exec(
                    id,
                    "sh",
                    "-c",
                    'chown agent:agent "$@" && find "$@" -maxdepth 1 -name lost+found -delete',
                    "sh",
                    *artifacts,
                    user="root",
                )
                # Restore tracked files (e.g. .worktrees/.gdignore) that the volumes hide,
                # so they don't appear deleted to the guest.
                self.engine.exec(
                    id,
                    "sh",
                    "-c",
                    'cd "$1" && shift && git ls-files -z -- "$@" | xargs -0 -r git checkout --',
                    "sh",
                    str(project.root),
                    *project.artifacts,
                )
            self.engine.exec(
                id,
                "tmux",
                "-u",
                "-f",
                "/home/agent/.devbox/tmux.conf",
                "new-session",
                "-d",
                "-s",
                "agent",
                "-c",
                str(project.cwd),
                "--",
                *command,
                env=secrets,
            )
        except (DevboxError, KeyboardInterrupt):
            # Retain state for diagnosis, but do not leak a VM or its volumes.
            self.engine.remove(id)
            raise
        print(
            f"Created {id} · {harness.name} · {project.cwd} "
            f"({time.monotonic() - started:.1f}s to start)",
            flush=True,
        )
        return id

    def attach(self, container: Container) -> None:
        if container.state != "running":
            raise DevboxError(
                f"{container.id} is stopped; resume a saved conversation in a new container"
            )
        if self.engine.exec(
            container.id, "tmux", "has-session", "-t", "agent", check=False
        ).returncode:
            raise DevboxError(
                "Harness session has exited. Use shell to inspect, or resume saved history."
            )
        print("Detach with Ctrl-a, then d. The harness will keep running.", flush=True)
        self.engine.connect(container.id, "tmux", "-u", "attach-session", "-t", "agent")

    def shell(self, container: Container) -> None:
        if container.state != "running":
            raise DevboxError(f"{container.id} is stopped")
        metadata = self.store.metadata(container.id)
        self.engine.connect(container.id, "/bin/bash", cwd=metadata["project"])

    def kill_all(self) -> int:
        containers = self.engine.containers()
        for container in containers:
            self.engine.remove(container.id)
        return len(containers)
