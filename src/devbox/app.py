"""Build composition and lifecycle. No project- or Pi-specific dependencies here."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from devbox import repository
from devbox.config import ASSETS, Harness, Project, configured_secrets, git, git_output, relative
from devbox.engine.engine import (
    IMAGE_BASE,
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
    shared_config,
    snapshot_session,
)

PREPARED = PurePosixPath("/opt/devbox/prepared")
PALETTE = (24, 25, 29, 53, 60, 88, 94, 96, 130)
# Named instances use their own color; dark enough for the white status text.
COLORS = {
    "red": 124,
    "orange": 166,
    "yellow": 136,
    "green": 28,
    "teal": 30,
    "blue": 25,
    "purple": 91,
    "pink": 162,
    "brown": 94,
    "gray": 240,
}
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
    """Declared host secrets, with nonempty environment values overriding devbox config."""
    return {
        **configured_secrets(harness),
        **{name: value for name in harness.secrets if (value := os.environ.get(name))},
    }


def accent(id: str) -> int:
    if (color := COLORS.get(id.removeprefix("devbox-"))) is not None:
        return color
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


def containerfile_base(file: Path) -> str:
    """The external image a Containerfile's final stage builds on, or "" if unknown."""
    defaults: dict[str, str] = {}
    stages: dict[str, str] = {}
    base = ""
    try:
        lines = file.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        words = line.split()
        if not words:
            continue
        if words[0].upper() == "ARG" and len(words) == 2 and "=" in words[1]:
            name, _, value = words[1].partition("=")
            defaults.setdefault(name, value.strip("\"'"))
        elif words[0].upper() == "FROM":
            args = [word for word in words[1:] if not word.startswith("--")]
            if not args:
                return ""
            base = re.sub(
                r"\$\{(\w+)\}|\$(\w+)", lambda m: defaults.get(m[1] or m[2], m[0]), args[0]
            )
            base = stages.get(base, base)
            if len(args) == 3 and args[1].upper() == "AS":
                stages[args[2]] = base
    return "" if "$" in base or base == "scratch" else base


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
            for kind, tag, file, source, layer_args, parent in (
                (
                    "project",
                    base,
                    project.containerfile,
                    project.context,
                    {},
                    containerfile_base(project.containerfile),
                ),
                (
                    "runtime",
                    runtime,
                    ASSETS / "runtime/Containerfile",
                    ASSETS / "runtime",
                    {"PROJECT_IMAGE": base},
                    base,
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
                    runtime,
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
                        IMAGE_BASE: parent,
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
        from_instance: bool = False,
    ) -> str:
        """from_instance clones the resumed session's instance instead of the host checkout."""
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
            print("Forwarding harness credentials: " + ", ".join(secrets), flush=True)
        shared_mounts = []
        for name in harness.shared_dirs:
            # Reserved for mounted state, never inherited from a project/profile.
            env.pop(f"DEVBOX_SHARED_{name.upper()}", None)
        if not no_config:
            for name, source in shared_config(harness, project_root=project.root).items():
                target = str(PurePosixPath("/home/agent") / harness.shared_dirs[name])
                shared_mounts.append((str(source), target))
                env[f"DEVBOX_SHARED_{name.upper()}"] = target
                print(f"Sharing {harness.name} {name}: {source}", flush=True)
        id, directory = self.store.create(
            f"devbox-{color}" for color in random.sample(list(COLORS), len(COLORS))
        )
        cloned = directory / "repo"
        previous = session.instance if session and from_instance else None
        branch, base, start = self._clone(project, cloned, previous)
        home = directory / "home"
        (home / harness.session_dir).mkdir(parents=True, exist_ok=True)
        if not no_config:
            imported = seed_config(harness, home)
            if imported:
                print("Seeded isolated config copy: " + ", ".join(imported), flush=True)
        run_hook(harness, "trust_project", home / harness.state_dir, str(project.cwd))
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
            (ASSETS / "runtime/environment.md")
            .read_text()
            .format(
                id=id,
                architecture=architecture,
                checkout=project.checkout,
                start=start,
            )
        )
        (home / ".devbox/tmux.conf").write_text(tmux_config(id, project.root.name, harness.name))
        metadata = {
            "id": id,
            "project": str(project.cwd),
            "root": str(project.root),
            "checkout": str(project.checkout),
            "branch": branch,
            "base": base,
            "harness": harness.name,
            "config": str(project.config),
            "session_dir": harness.session_dir,
            "session_glob": harness.session_glob,
            "image": image,
            "resumed_from": session.id if session else None,
        }
        (directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
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
            binds=(
                (str(cloned), str(project.checkout)),
                (str(home), "/home/agent"),
                *shared_mounts,
            ),
            env={**env, **git_identity(project.cwd), "DEVBOX_INSTANCE": id},
        )
        started = time.monotonic()
        try:
            self.engine.run(spec)
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
            # Retain state for diagnosis, but do not leak a container.
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

    def _clone(
        self, project: Project, target: Path, previous: str | None
    ) -> tuple[str | None, str, str]:
        """Return the host branch and commit the work started from, and what is checked out."""
        if previous:
            source = repository.git_directory(self.store.directory(previous) / "repo")
            metadata = self.store.metadata(previous)
            branch, head = repository.clone(source, target)
            # Keep the original host starting point for provenance and sync bundles.
            return metadata.get("branch"), metadata["base"], branch or head[:12]
        if git_output(project.checkout, "rev-parse", "--verify", "HEAD^{commit}") is None:
            raise DevboxError(f"{project.checkout} must be a Git repository with a commit")
        if git_output(project.checkout, "status", "--porcelain", "--untracked-files=no"):
            print(f"Uncommitted changes in {project.checkout} are not copied.", flush=True)
        branch = git_output(project.checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
        # Cloning the checkout, not the primary, transfers its HEAD even when it is detached.
        _, head = repository.clone(project.checkout, target)
        if branch is None:
            # Clone guesses a branch at the same commit; the host checkout is detached.
            git(target, "checkout", "--quiet", "--detach")
        return branch, head, branch or head[:12]

    def fetch(self, id: str) -> list[repository.Fetched]:
        """Fetch instance refs and publish new work as <short-instance>/<branch>."""
        metadata = self.store.metadata(id)
        clone = self.store.directory(id) / "repo"
        # The host's manifest, not the clone's, decides which changes need attention.
        project = Project.discover(Path(metadata["config"]).parent)
        # Repository-relative paths: the same in every worktree of the host repository.
        watched = [
            f":(literal){path.relative_to(project.checkout)}"
            for path in (project.config.resolve(), project.containerfile, project.context)
            if path.is_relative_to(project.checkout)
        ] + [f":(glob){pattern}" for pattern in project.watch]
        return repository.fetch(
            project.root,
            clone,
            f"refs/devbox/{id}/",
            watched,
            branch_prefix=id.removeprefix("devbox-"),
        )

    def review(self, id: str, ref: str, *options: str) -> None:
        """Show a fetched branch's changes since its merge base with the current host HEAD."""
        metadata = self.store.metadata(id)
        repository.show_diff(Path(metadata["root"]), ref, *options)

    def land(self, id: str, branch: repository.Fetched) -> repository.Landed:
        """Land a fetched branch in the instance's host repository's primary checkout."""
        metadata = self.store.metadata(id)
        return repository.land(Path(metadata["root"]), branch)

    def sync_landed(
        self, id: str, fetched: repository.Fetched, landed: repository.Landed
    ) -> list[tuple[str, list[str]]]:
        """Sync the landed target to every instance of the project; never fails the landing.

        The landing instance's own branch also moves to its rebased commits, so its history
        matches what was landed. Returns a report per instance.
        """
        root = self.store.metadata(id)["root"]
        branch = fetched.ref.removeprefix(f"refs/devbox/{id}/")
        try:
            self.engine.require_running()
            containers = self.engine.containers()
        except DevboxError as exc:
            return [(id, [f"not synced: {exc}"])]
        report = []
        for container in containers:
            try:
                if self.store.metadata(container.id)["root"] != root:
                    continue
            except DevboxError:
                continue  # Not one of ours, or its saved state is gone.
            try:
                if container.state != "running":
                    report.append((container.id, ["stopped, not synced; run devbox sync later"]))
                    continue
                rewrites = [(branch, landed.fetched, landed.rebased)] if container.id == id else []
                report.append((container.id, self.sync(container, [landed.target], rewrites)))
            except DevboxError as exc:
                report.append((container.id, [f"not synced: {exc}"]))
        return report

    def sync(
        self,
        container: Container,
        branches: list[str],
        rewrites: Sequence[tuple[str, str, str]] = (),
    ) -> list[str]:
        """Deliver host branches to a running instance as host/<branch>, then update its
        local branches where that loses nothing; returns one report line per branch.

        The bundle is built in the host repository and unpacked by Git in the guest, which
        also decides what is safe to move (see repository.RECEIVE). Each rewrite is a
        (branch, old, new) replacement that applies only while the branch is still at old.
        """
        if container.state != "running":
            raise DevboxError(f"{container.id} is stopped; sync needs a running container")
        metadata = self.store.metadata(container.id)
        branches = branches or ([metadata["branch"]] if metadata.get("branch") else [])
        if not branches:
            raise DevboxError(f"{container.id} started from a detached HEAD; name branches to sync")
        data = repository.bundle(Path(metadata["root"]), branches, metadata["base"])
        # Rewrites first: a rebased branch must not be reported as diverged from its target.
        specs = [f"rewrite:{name}:{old}:{new}" for name, old, new in rewrites]
        specs += [f"ff:{branch}" for branch in branches]
        result = self.engine.exec(
            container.id,
            "sh",
            "-c",
            repository.RECEIVE,
            "sh",
            metadata["checkout"],
            *specs,
            input=data,
        )
        return result.stdout.splitlines()

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
        by_identity = {identity: image for image in images for identity in image.identities}
        unused = []
        for image in owned:
            if image.identities & protected:
                continue
            # Removing an image leaves its base in place, so only report what it adds.
            base = by_identity.get(image_identity(image.labels.get(IMAGE_BASE, "")))
            if image.size is not None and base and base.size is not None and base.id != image.id:
                image = replace(image, size=max(image.size - base.size, 0))
            unused.append(image)
        return sorted(unused, key=lambda image: image.reference)

    def cleanup_images(self, images: list[Image]) -> tuple[int, dict[str, int | None]]:
        removed = 0
        sizes: dict[str, int | None] = {}
        # Recheck each deletion: new sessions/containers must protect an image even
        # if they appeared during confirmation or an earlier deletion.
        for candidate in dict.fromkeys((image.reference, image.id) for image in images):
            current = {(image.reference, image.id): image for image in self.unused_images()}
            if candidate in current:
                image = current[candidate]
                self.engine.remove_image(image.reference)
                removed += 1
                sizes[image_identity(image.id)] = image.size
        if not removed:
            return 0, {}
        # Removing a tag need not remove its image. Count each ID only once, and only
        # after its last reference disappears, including aliases we do not own.
        remaining = {image_identity(image.id) for image in self.engine.images()}
        return removed, {id: size for id, size in sizes.items() if id not in remaining}

    def kill_all(self) -> int:
        containers = self.engine.containers()
        for container in containers:
            self.engine.remove(container.id)
        return len(containers)
