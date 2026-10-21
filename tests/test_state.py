import json
from dataclasses import replace
from pathlib import Path

import pytest

from agentbox.config import Harness
from agentbox.errors import AgentboxError
from agentbox.state import Store, seed_config, snapshot_session


def test_seed_is_copy_not_shared_state(tmp_path: Path, harness: Harness) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "private"}')
    (source / "settings.json").write_text(
        json.dumps(
            {
                "defaultModel": "model",
                "sessionDir": "/host/sessions",
                "shellPath": "/opt/homebrew/bin/bash",
                "extensions": ["/host/extension.ts"],
                "packages": [
                    "npm:context-mode",
                    {"source": "git:github.com/example/pkg@v1", "skills": []},
                    "/host/package",
                ],
            }
        )
    )
    (source / "mcp.json").write_text('{"secret": "must not copy"}')
    home = tmp_path / "home"
    messages = seed_config(replace(harness, seed_dir=source), home)
    target = home / harness.state_dir
    settings = json.loads((target / "settings.json").read_text())
    assert "sessionDir" not in settings
    assert "shellPath" not in settings
    assert "extensions" not in settings
    assert settings["defaultModel"] == "model"
    assert len(settings["packages"]) == 2
    assert not (target / "mcp.json").exists()
    assert "private" not in str(messages)
    (target / "auth.json").write_text("changed")
    assert "private" in (source / "auth.json").read_text()


def test_seed_skips_symlink_files(tmp_path: Path, harness: Harness) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("private")
    (source / "auth.json").symlink_to(outside)
    seed_config(replace(harness, seed_dir=source), tmp_path / "home")
    assert not (tmp_path / "home" / harness.state_dir / "auth.json").exists()


def test_snapshot_ignores_incomplete_record(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b'{"type":"session"}\n{"partial":')
    snapshot_session(source, target)
    assert target.read_bytes() == b'{"type":"session"}\n'
    assert b"partial" in source.read_bytes()


def test_empty_snapshot_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("partial")
    with pytest.raises(AgentboxError, match="no complete"):
        snapshot_session(source, tmp_path / "target")


def test_catalog_survives_without_a_container(tmp_path: Path) -> None:
    store = Store(tmp_path)
    id, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps({"harness": "pi", "project": "/project", "session_dir": ".pi/agent/sessions"})
    )
    sessions = directory / "home/.pi/agent/sessions"
    sessions.mkdir(parents=True)
    (sessions / "valid.jsonl").write_text(
        '{"type":"session","id":"conversation"}\n{"text":"secret"}\n'
    )
    (sessions / "broken.jsonl").write_text("not json")
    rows = store.sessions()
    assert len(rows) == 1
    assert rows[0].id == f"{id}/conversation"
    assert "secret" not in str(rows)


def test_unknown_instance(tmp_path: Path) -> None:
    with pytest.raises(AgentboxError, match="No saved state"):
        Store(tmp_path).directory("agentbox-missing")
