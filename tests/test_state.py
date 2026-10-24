import json
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.config import Harness
from devbox.errors import DevboxError
from devbox.state import Store, seed_config, snapshot_session


@pytest.mark.parametrize("platform,backend", [("darwin", "apple"), ("linux", "podman")])
def test_store_uses_devbox_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, backend: str
) -> None:
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = Store()
    assert store.root == tmp_path / "data/devbox" / backend
    instance, directory = store.create()
    assert instance.startswith("devbox-")
    assert directory == store.root / "instances" / instance
    assert (directory / "home").is_dir()


def test_store_scopes_custom_roots_and_leaves_legacy_state_untouched(tmp_path: Path) -> None:
    legacy = tmp_path / "instances/devbox-old/home"
    legacy.mkdir(parents=True)
    (legacy / "conversation.jsonl").write_text("saved conversation")
    apple = Store(tmp_path, backend="apple")
    podman = Store(tmp_path, backend="podman")
    id, directory = apple.create()
    assert directory == tmp_path / "apple/instances" / id
    assert [instance.id for instance in apple.orphaned(set())] == [id]
    assert podman.orphaned(set()) == []
    assert podman.sessions() == []
    assert podman.image_references(set()) == set()
    with pytest.raises(DevboxError, match="Not a removable instance"):
        podman.remove_instance(id)
    assert (legacy / "conversation.jsonl").read_text() == "saved conversation"
    assert directory.is_dir()


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
    assert not (target / "auth.json").exists()
    (target / "settings.json").write_text("changed")
    assert "defaultModel" in (source / "settings.json").read_text()
    assert "private" in (source / "auth.json").read_text()


def test_seed_skips_symlink_files(tmp_path: Path, harness: Harness) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("private")
    (source / "keybindings.json").symlink_to(outside)
    seed_config(replace(harness, seed_dir=source), tmp_path / "home")
    assert not (tmp_path / "home" / harness.state_dir / "keybindings.json").exists()


def test_snapshot_ignores_incomplete_record(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b'{"type":"session"}\n{"partial":')
    snapshot_session(source, target)
    assert target.read_bytes() == b'{"type":"session"}\n'
    assert b"partial" in source.read_bytes()


def test_empty_snapshot_fails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_text("partial")
    with pytest.raises(DevboxError, match="no complete"):
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


def test_catalog_reads_only_headers_without_loading_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("Session listing must not load profiles or hooks")

    monkeypatch.setattr(Harness, "load", unexpected)
    monkeypatch.setattr(Harness, "hook", unexpected)
    store = Store(tmp_path)
    id, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps({"harness": "unknown", "project": "/project", "session_dir": "sessions"})
    )
    sessions = directory / "home/sessions"
    sessions.mkdir()
    (sessions / "valid.jsonl").write_bytes(
        b'{"type":"session","id":"conversation"}\n' + b"\xff" * 100_000
    )
    (row,) = store.sessions()
    assert row.id == f"{id}/conversation"
    assert row.harness == "unknown"


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
    with pytest.raises(DevboxError, match="No saved state"):
        Store(tmp_path).directory("devbox-missing")
