"""Host-side Pi adaptations: portable package declarations and settings cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PORTABLE = ("npm:", "git:", "https://", "http://")
# These refer to host resources or could redirect durable session storage.
HOST_SETTINGS = (
    "sessionDir",
    "shellPath",
    "shellCommandPrefix",
    "npmCommand",
    "externalEditor",
    "extensions",
    "skills",
    "prompts",
    "themes",
)


def _settings(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("packages", []), list):
            raise ValueError
        return data
    except ValueError:
        raise ValueError("Pi settings.json is invalid; fix it or use --no-config") from None


def _source(package: Any) -> str:
    source = package.get("source") if isinstance(package, dict) else package
    if not isinstance(source, str):
        raise ValueError("Pi settings.json is invalid; fix it or use --no-config")
    return source


def build_files(seed_dir: Path) -> dict[str, str]:
    """Package declarations to bake into the image; never settings or auth."""
    path = seed_dir / "settings.json"
    if path.is_symlink() or not path.is_file():
        return {}
    sources = set()
    for package in _settings(path).get("packages", []):
        source = _source(package)
        if not source.startswith(PORTABLE):
            continue
        # Declarations are visible in image layers: refuse to bake URL credentials.
        url = urlsplit(source.removeprefix("git:"))
        if url.username or url.password or url.query or any(c.isspace() for c in source):
            raise ValueError(f"Refusing to bake package source into an image: {source!r}")
        sources.add(source)
    return {"package-sources.txt": "".join(f"{source}\n" for source in sorted(sources))}


def adapt_seed(state_dir: Path) -> list[str]:
    """Drop host-specific settings from the copied settings.json."""
    path = state_dir / "settings.json"
    if not path.is_file():
        return []
    data = _settings(path)
    notes = []
    for key in HOST_SETTINGS:
        if data.pop(key, None) is not None:
            notes.append(f"omitted host-specific setting {key}")
    packages = []
    for package in data.get("packages", []):
        if _source(package).startswith(PORTABLE):
            packages.append(package)
        else:
            notes.append(f"omitted local package {_source(package)}")
    data["packages"] = packages
    path.write_text(json.dumps(data, indent=2) + "\n")
    return notes
