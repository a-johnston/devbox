"""A deliberately small adapter to rootless Podman."""

from __future__ import annotations

import json
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from devbox.engine import MANAGED, Container, Engine, Image, RunSpec, image_labels, image_strings
from devbox.errors import DevboxError

INSTANCE = "dev.devbox.instance"
# Map the host user onto the guest's agent user, so files it writes stay owned by the host user.
AGENT_USERNS = "keep-id:uid=10001,gid=10001"


class PodmanEngine(Engine):
    name = "podman"
    executable = "podman"
    install_hint = "Podman is required: https://podman.io/docs/installation"
    isolation = "container"
    run_options = ("--userns", AGENT_USERNS)
    # Rootless cgroupfs cannot delegate controllers, so Podman silently drops --cpus/--memory.
    limits_ignored = False

    def require_running(self) -> None:
        result = self.call("info", "--format", "json", check=False)
        if result.returncode:
            raise DevboxError(f"podman info failed: {result.stderr.strip()}")
        try:
            host = json.loads(result.stdout)["host"]
            rootless = host["security"]["rootless"]
            manager = host["cgroupManager"]
            version = host["cgroupVersion"]
            if type(rootless) is not bool or manager not in ("systemd", "cgroupfs"):
                raise ValueError("Unrecognized Podman configuration")
            if not isinstance(version, str):
                raise ValueError("Expected cgroup version")
        except (ValueError, KeyError, TypeError) as exc:
            raise DevboxError("Cannot validate rootless setup from podman info") from exc
        if not rootless:
            raise DevboxError("Devbox requires rootless Podman; run it as your regular user")
        if version != "v2":
            raise DevboxError("Devbox requires Podman with cgroup v2")
        self.limits_ignored = manager == "cgroupfs"

    def image_exists(self, reference: str) -> bool:
        result = self.call("image", "exists", reference, check=False)
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise DevboxError(f"Cannot check prepared image: {result.stderr.strip()}")

    @contextmanager
    def building(self) -> Generator[None]:
        yield

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
        flags = [
            *(flag for key, value in args.items() for flag in ("--build-arg", f"{key}={value}")),
            *(flag for key, value in labels.items() for flag in ("--label", f"{key}={value}")),
        ]
        self.call(
            "build",
            "--platform",
            platform,
            "--tag",
            tag,
            "--file",
            str(file),
            *flags,
            str(context),
            capture=False,
        )

    def images(self) -> list[Image]:
        result = self.call("image", "list", "--all", "--no-trunc", "--format", "json")
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                raise ValueError("Expected images")
            ids = image_strings([row["Id"] for row in rows])
            images = []
            # Inspect includes all tags, digests and config labels, even for dangling images.
            for id in dict.fromkeys(ids):
                result = self.call("image", "inspect", id)
                inspected = json.loads(result.stdout)
                if not isinstance(inspected, list) or len(inspected) != 1:
                    raise ValueError("Expected one inspected image")
                row = inspected[0]
                (actual_id,) = image_strings([row["Id"]])
                if actual_id.removeprefix("sha256:") != id.removeprefix("sha256:"):
                    raise ValueError("Image identity changed")
                tags = image_strings(row.get("RepoTags") or [])
                digests = image_strings(row.get("RepoDigests") or [])
                labels = image_labels(row["Config"])
                for reference in tags or (id,):
                    images.append(Image(reference, actual_id, labels, (*tags, *digests)))
            return images
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DevboxError("Cannot safely determine existing images; refusing cleanup") from exc

    def container_images(self) -> set[str]:
        try:
            return set(
                image_strings(
                    [
                        value
                        for row in self._rows("--no-trunc")
                        for value in (row["Image"], row["ImageID"])
                    ]
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DevboxError("Cannot safely determine container images; refusing cleanup") from exc

    def remove_image(self, reference: str) -> None:
        self.call("image", "rm", "--", reference)

    def run(self, spec: RunSpec) -> None:
        # Labelled up front, so removal finds them even if the container never starts.
        for name, _ in spec.volumes:
            self.call(
                "volume",
                "create",
                "--label",
                f"{MANAGED}=1",
                "--label",
                f"{INSTANCE}={spec.name}",
                name,
            )
        if self.limits_ignored:
            print(
                "Warning: CPU and memory limits are not enforced with Podman's cgroupfs manager;"
                " reach systemd's user bus to enforce them.",
                file=sys.stderr,
                flush=True,
            )
        try:
            super().run(spec)
        except DevboxError as exc:
            if "org.freedesktop.systemd1" not in str(exc):
                raise
            raise DevboxError(
                f"{exc}\nPodman could not reach systemd's user bus to create the container's "
                "cgroup. Check that DBUS_SESSION_BUS_ADDRESS is unix:path=$XDG_RUNTIME_DIR/bus "
                "(a desktop started with dbus-run-session gets a private bus instead). Setting "
                '[engine] cgroup_manager = "cgroupfs" in ~/.config/containers/containers.conf '
                "also works, but then CPU and memory limits are not enforced."
            ) from exc

    def _rows(self, *filters: str) -> list[dict[str, Any]]:
        result = self.call("ps", "--all", *filters, "--format", "json")
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("Names"), list)
            or not row["Names"]
            or not isinstance(row["Names"][0], str)
            or not row["Names"][0]
            for row in rows
        ):
            raise ValueError("Expected container rows")
        return rows

    def container_ids(self) -> set[str]:
        try:
            return {row["Names"][0] for row in self._rows()}
        except (ValueError, TypeError) as exc:
            raise DevboxError(
                "Cannot safely determine existing containers; refusing cleanup"
            ) from exc

    def containers(self) -> list[Container]:
        try:
            containers = []
            for row in self._rows("--filter", f"label={MANAGED}=1"):
                labels = row.get("Labels") or {}
                if labels.get(MANAGED) != "1":
                    continue
                created = row.get("Created")
                containers.append(
                    Container(
                        row["Names"][0],
                        row["State"],
                        labels.get("dev.devbox.project", "?"),
                        labels.get("dev.devbox.harness", "?"),
                        datetime.fromtimestamp(created).astimezone().isoformat(timespec="seconds")
                        if isinstance(created, int)
                        else "?",
                    )
                )
            return containers
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DevboxError("Unexpected podman JSON") from exc

    def remove(self, id: str) -> None:
        """Volumes carry their container's name as a label; --ignore covers a failed start."""
        self.call("stop", id, check=False)
        self.call("rm", "--ignore", id)
        result = self.call(
            "volume", "ls", "--filter", f"label={INSTANCE}={id}", "--format", "{{.Name}}"
        )
        for volume in result.stdout.split():
            self.call("volume", "rm", volume)
