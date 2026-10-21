"""Host-side Claude Code adaptations: settings cleanup and resume placement."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

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


def session_title(records: Iterable[dict[str, Any]]) -> str | None:
    """Prefer an explicit /rename over Claude's automatic title or session summary."""
    titles = {}
    fields = {"custom-title": "customTitle", "ai-title": "aiTitle", "summary": "summary"}
    for record in records:
        kind = record.get("type")
        if not isinstance(kind, str) or kind not in fields:
            continue
        value = record.get(fields[kind])
        if isinstance(value, str) and value.strip():
            titles[kind] = value
    return titles.get("custom-title") or titles.get("ai-title") or titles.get("summary")


def adapt_seed(state_dir: Path) -> list[str]:
    """Skip first-run login so a stored token applies; drop host-specific settings."""
    # Onboarding insists on its own login flow even when CLAUDE_CODE_OAUTH_TOKEN is set.
    # Without a token, Claude still starts and offers /login.
    (state_dir / ".claude.json").write_text('{"hasCompletedOnboarding": true}\n')
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
