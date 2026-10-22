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
from devbox.engine import (
    IMAGE_KIND,
    IMAGE_SCHEMA,
    IMAGE_STORE,
    MANAGED,
    Container,
    Engine,
    Image,
    RunSpec,
    host_platform,
    image_identity,
    select_engine,
)
from devbox.errors import DevboxError
from devbox.state import (
    Instance,
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
        self.engine = engine or select_engine()
        self.store = store or Store(backend=self.engine.name)
        if self.store.backend != self.engine.name:
            raise DevboxError("The saved-state backend must match the container engine")

    @property
    def image_owner(self) -> dict[str, str]:
        return {
            MANAGED: "1",
            IMAGE_STORE: hashlib.sha256(str(self.store.root).encode()).hexdigest(),
            IMAGE_SCHEMA: "1",
        }

    def _image_key(self, project: Project) -> str:
        return hashlib.sha256(f"{self.store.root}:{project.root}".encode()).hexdigest()[:12]

    def _prepared_image(
        self, project: Project, harness: Harness, files: dict[str, str], args: dict[str, str]
    ) -> str:
        # A separate namespace prevents old, unprepared images from taking the fast path.
        inputs = json.dumps(
            {
                "files": files,
                "args": args,
                "platform": host_platform(),
                "ownership": self.image_owner,
            },
            sort_keys=True,
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
        with (
            self.engine.building(),
            tempfile.TemporaryDirectory(prefix="devbox-build-") as temp,
        ):
            # Only harness assets and hook-selected public declarations enter this context.
            context = Path(temp) / "harness"
            shutil.copytree(
                harness.directory, context, ignore=shutil.ignore_patterns("__pycache__")
            )
            for name, content in files.items():
                (context / name).write_text(content)
            for kind, tag, file, source, layer_args in (
                ("project", base, project.containerfile, project.context, {}),
                (
                    "runtime",
                    runtime,
                    ASSETS / "runtime/Containerfile",
                    ASSETS / "runtime",
                    {"PROJECT_IMAGE": base},
                ),
                (
                    "prepared",
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
                    cpus=project.cpus,
                    labels={
                        **self.image_owner,
                        IMAGE_KIND: kind,
                        "dev.devbox.project": str(project.root),
                        "dev.devbox.harness": harness.name if kind == "prepared" else "",
                    },
                )
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
            print("Using prepared image; no build needed.", flush=True)
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
            f"You are running in devbox instance {id}, a Linux {architecture} {self.engine.isolation}, as a non-root user.\n"
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
        artifacts = [str(project.root / artifact) for artifact in project.artifacts]
        for artifact in artifacts:
            # Engines create missing mount points; rootless Podman would leave them owned by
            # a subordinate UID, blocking host builds there.
            Path(artifact).mkdir(parents=True, exist_ok=True)
        spec = RunSpec(
            name=id,
            image=image,
            cpus=project.cpus,
            memory=project.memory,
            labels={
                MANAGED: "1",
                "dev.devbox.project": str(project.root),
                "dev.devbox.harness": harness.name,
            },
            binds=((str(project.root), str(project.root)), (str(home), "/home/agent")),
            volumes=tuple((f"{id}-{i}", target) for i, target in enumerate(artifacts)),
            env={**env, **git_identity(project.cwd)},
        )
        started = time.monotonic()
        try:
            self.engine.run(spec)
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
            # Retain state for diagnosis, but do not leak a container or its volumes.
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

    def orphaned_instances(self) -> list[Instance]:
        self.engine.require_running()
        return self.store.orphaned(self.engine.container_ids())

    def cleanup(self, instances: list[Instance]) -> tuple[int, int]:
        # Recheck after confirmation. Never add newly discovered instances to the selection.
        current = {instance.id: instance for instance in self.orphaned_instances()}
        removed = recovered = 0
        for id in dict.fromkeys(instance.id for instance in instances):
            if id in current:
                self.store.remove_instance(id)
                removed += 1
                recovered += current[id].size
        return removed, recovered

    def unused_images(self, removing: list[Instance] | None = None) -> list[Image]:
        self.engine.require_running()
        images = self.engine.images()
        owned = [
            image
            for image in images
            if all(image.labels.get(key) == value for key, value in self.image_owner.items())
            and image.labels.get(IMAGE_KIND) in {"project", "runtime", "prepared"}
        ]
        if not owned:
            return []
        references = self.store.image_references({instance.id for instance in removing or []})
        references.update(self.engine.container_images())
        protected = {image_identity(reference) for reference in references}
        # Protect every alias of a referenced image, including other registry/tag spellings.
        for image in images:
            if image.identities & protected:
                protected.update(image.identities)
        return sorted(
            (image for image in owned if not image.identities & protected),
            key=lambda image: image.reference,
        )

    def cleanup_images(self, images: list[Image]) -> int:
        removed = 0
        # Recheck each deletion: new sessions/containers must protect an image even
        # if they appeared during confirmation or an earlier deletion.
        for candidate in dict.fromkeys((image.reference, image.id) for image in images):
            current = {(image.reference, image.id) for image in self.unused_images()}
            if candidate in current:
                self.engine.remove_image(candidate[0])
                removed += 1
        return removed

    def kill_all(self) -> int:
        containers = self.engine.containers()
        for container in containers:
            self.engine.remove(container.id)
        return len(containers)
