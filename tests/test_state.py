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


@pytest.mark.parametrize(
    ("harness_name", "records", "expected"),
    [
        ("pi", [{"type": "session_info", "name": "Fix login"}], "Fix login"),
        (
            "pi",
            [
                {"type": "session_info", "name": "Old name"},
                {"type": "session_info", "name": "New name"},
            ],
            "New name",
        ),
        ("claude", [{"type": "summary", "summary": "Fix login"}], "Fix login"),
        (
            "claude",
            [
                {"type": "custom-title", "customTitle": "My title"},
                {"type": "summary", "summary": "Generated title"},
            ],
            "My title",
        ),
        (
            "claude",
            [
                {"type": "summary", "summary": "Old summary"},
                {"type": "ai-title", "aiTitle": "Automatic title"},
                {"type": "custom-title", "customTitle": "Old title"},
                {"type": "custom-title", "customTitle": "New title"},
            ],
            "New title",
        ),
        ("claude", [{"type": "ai-title", "aiTitle": "Automatic title"}], "Automatic title"),
        ("pi", [{"type": "message", "message": {"role": "user", "content": "secret"}}], None),
        ("pi", [{"type": "session_info", "name": 42}], None),
        ("pi", [{"type": "session_info", "name": " \n\t "}], None),
        ("pi", [{"type": "session_info", "name": "Hello\n\tworld\x1b\x00"}], "Hello world"),
        ("pi", [{"type": "session_info", "name": "a" * 200}], "a" * 97 + "..."),
        ("unknown", [{"type": "session_info", "name": "Not a Pi session"}], None),
    ],
)
def test_catalog_session_titles(
    tmp_path: Path, harness_name: str, records: list[dict], expected: str | None
) -> None:
    store = Store(tmp_path)
    id, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps({"harness": harness_name, "project": "/project", "session_dir": "sessions"})
    )
    sessions = directory / "home/sessions"
    sessions.mkdir()
    (sessions / "valid.jsonl").write_text(
        json.dumps({"type": "session", "id": "conversation"})
        + "\n"
        + "".join(json.dumps(record) + "\n" for record in records)
    )
    (row,) = store.sessions()
    assert row.id == f"{id}/conversation"
    assert row.title == expected
    assert "secret" not in str(row)


def test_catalog_title_ignores_invalid_and_incomplete_records(tmp_path: Path) -> None:
    store = Store(tmp_path)
    _, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps({"harness": "pi", "project": "/project", "session_dir": "sessions"})
    )
    sessions = directory / "home/sessions"
    sessions.mkdir()
    (sessions / "valid.jsonl").write_text(
        '{"type":"session","id":"conversation"}\n'
        "not json\n[]\nnull\n"
        + json.dumps({"type": "message", "content": "x" * 100_000})
        + '\n{"type":"session_info","name":"Complete title"}\n'
        '{"type":"session_info","name":"Incomplete title"}'
    )
    (row,) = store.sessions()
    assert row.title == "Complete title"


def test_catalog_does_not_follow_session_symlinks(tmp_path: Path) -> None:
    store = Store(tmp_path / "state")
    _, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps({"harness": "pi", "project": "/project", "session_dir": "sessions"})
    )
    sessions = directory / "home/sessions"
    sessions.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text('{"type":"session","id":"secret"}\n')
    (sessions / "linked.jsonl").symlink_to(outside)
    assert store.sessions() == []


def test_unknown_instance(tmp_path: Path) -> None:
    with pytest.raises(AgentboxError, match="No saved state"):
        Store(tmp_path).directory("agentbox-missing")
