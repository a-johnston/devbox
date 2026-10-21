"""Host-side Claude Code adaptations: settings cleanup and resume placement."""

from __future__ import annotations

import json
import re
from pathlib import Path

# These run host commands, refer to host paths or need host-installed plugins.
HOST_SETTINGS = (
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "otelHeadersHelper",
    "hooks",
    "statusLine",
    "enabledPlugins",
    "extraKnownMarketplaces",
)


def adapt_seed(state_dir: Path) -> list[str]:
    """Drop host-specific settings from the copied settings.json."""
    path = state_dir / "settings.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        raise ValueError("Claude settings.json is invalid; fix it or use --no-config") from None
    notes = []
    for key in HOST_SETTINGS:
        if data.pop(key, None) is not None:
            notes.append(f"omitted host-specific setting {key}")
    permissions = data.get("permissions")
    if isinstance(permissions, dict) and permissions.pop("additionalDirectories", None):
        notes.append("omitted host-specific setting permissions.additionalDirectories")
    path.write_text(json.dumps(data, indent=2) + "\n")
    return notes


def resume_file(session_id: str, cwd: str) -> str:
    """Claude looks up --resume IDs in the project directory keyed by the working directory."""
    return f".claude/projects/{re.sub(r'[^A-Za-z0-9]', '-', cwd)}/{session_id}.jsonl"
