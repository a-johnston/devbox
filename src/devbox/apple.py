"""A deliberately small adapter to Apple's container CLI."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from devbox.config import size
from devbox.engine import MANAGED, Container, Engine, Image, image_labels, image_strings
from devbox.errors import DevboxError


def builder_memory() -> str:
    """A host concern, not a project one: the builder is shared by every project."""
    return size(os.environ.get("DEVBOX_BUILDER_MEMORY", "4G"), "DEVBOX_BUILDER_MEMORY")


class AppleEngine(Engine):
    name = "apple"
    executable = "container"
    install_hint = "Apple's container CLI is required: https://github.com/apple/container"
    isolation = "VM"

    def require_running(self) -> None:
        result = self.call("system", "status", check=False)
        if not result.returncode and "not running" not in result.stdout:
            return
        message = "Start Apple's container service first: container system start"
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise DevboxError(message)
        try:
            answer = input("Apple's container service isn't running. Start it now? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            raise DevboxError(message)
        self.call("system", "start", capture=False)
        result = self.call("system", "status", check=False)
        if result.returncode or "not running" in result.stdout:
            detail = (result.stderr or result.stdout).strip()
            raise DevboxError(
                "Apple's container service is still unavailable after container system start"
                + (f": {detail}" if detail else "")
            )

    def image_exists(self, reference: str) -> bool:
        result = self.call("image", "inspect", reference, check=False)
        if result.returncode == 0:
            return True
        if "image not found:" in result.stderr.lower():
            return False
        raise DevboxError(f"Cannot check prepared image: {result.stderr.strip()}")

    @contextmanager
    def building(self) -> Generator[None]:
        # Stop a builder we start: it holds gigabytes of RAM and launches don't need it.
        started = not self.builder_running()
        try:
            yield
        finally:
            if started:
                self.call("builder", "stop", check=False)

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
        """Starts the builder if needed; the resources apply to (and may restart) it."""
        flags = [
            *(flag for key, value in args.items() for flag in ("--build-arg", f"{key}={value}")),
            *(flag for key, value in labels.items() for flag in ("--label", f"{key}={value}")),
        ]
        self.call(
            "build",
            "--platform",
            platform,
            "--memory",
            builder_memory(),
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

    def images(self) -> list[Image]:
        result = self.call("image", "list", "--format", "json")
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                raise ValueError("Expected images")
            images = []
            for row in rows:
                config = row["configuration"]
                reference, id = image_strings([config["name"], config["descriptor"]["digest"]])
                variants = row["variants"]
                if not isinstance(variants, list) or not variants:
                    raise ValueError("Expected image variants")
                # Build labels live in OCI config, NOT descriptor annotations.
                # Ignore attestations, but require consistent labels on all runnable platforms.
                labels = []
                aliases = []
                for variant in variants:
                    aliases.append(variant["digest"])
                    if variant["platform"]["os"] == "unknown":
                        continue
                    labels.append(image_labels(variant["config"].get("config")))
                image_strings(aliases)
                if not labels:
                    raise ValueError("Expected runnable image variant")
                common = {
                    key: value
                    for key, value in labels[0].items()
                    if all(label.get(key) == value for label in labels)
                }
                images.append(Image(reference, id, common, tuple(aliases)))
            return images
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DevboxError("Cannot safely determine existing images; refusing cleanup") from exc

    def container_images(self) -> set[str]:
        result = self.call("list", "--all", "--format", "json")
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                raise ValueError("Expected containers")
            return set(
                image_strings(
                    [
                        value
                        for row in rows
                        for value in (
                            row["configuration"]["image"]["reference"],
                            row["configuration"]["image"]["descriptor"]["digest"],
                        )
                    ]
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DevboxError("Cannot safely determine container images; refusing cleanup") from exc

    def remove_image(self, reference: str) -> None:
        self.call("image", "delete", "--", reference)

    def builder_running(self) -> bool:
        result = self.call("builder", "status", "--format", "json", check=False)
        try:
            return any(row["status"]["state"] == "running" for row in json.loads(result.stdout))
        except (ValueError, KeyError, TypeError):
            return False

    def container_ids(self) -> set[str]:
        result = self.call("list", "--all", "--format", "json")
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]
                for row in rows
            ):
                raise ValueError("Expected container IDs")
            return {row["id"] for row in rows}
        except (ValueError, TypeError) as exc:
            raise DevboxError(
                "Cannot safely determine existing containers; refusing cleanup"
            ) from exc

    def containers(self) -> list[Container]:
        result = self.call("list", "--all", "--format", "json")
        try:
            rows: list[dict[str, Any]] = json.loads(result.stdout)
            return [
                Container(
                    row["id"],
                    row["status"]["state"],
                    row["configuration"]["labels"].get("dev.devbox.project", "?"),
                    row["configuration"]["labels"].get("dev.devbox.harness", "?"),
                    row["configuration"].get("creationDate", "?"),
                )
                for row in rows
                if row["configuration"].get("labels", {}).get(MANAGED) == "1"
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise DevboxError("Unexpected container JSON; devbox requires container 1.5+") from exc

    def remove(self, id: str) -> None:
        """Volumes are named after their container."""
        self.call("stop", id, check=False)
        self.call("delete", id)
        for volume in self.volumes(id):
            self.call("volume", "delete", volume)

    def volumes(self, id: str) -> list[str]:
        result = self.call("volume", "list", "--format", "json")
        try:
            names = [row["id"] for row in json.loads(result.stdout)]
        except (ValueError, KeyError, TypeError) as exc:
            raise DevboxError("Unexpected volume JSON") from exc
        return [name for name in names if name.startswith(f"{id}-")]
