import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from devbox.app import App
from devbox.config import ASSETS, Harness, Project
from devbox.errors import DevboxError
from devbox.state import Session, Store, seed_config, shared_config
from tests.fakes import FakeEngine


@pytest.fixture
def claude(tmp_path: Path) -> Harness:
    return replace(
        Harness.load(ASSETS / "harnesses/claude"),
        seed_dir=tmp_path / "host-config",
        version_command=(),
        secrets=(),
    )


@pytest.mark.parametrize("no_config", [False, True])
def test_new_skips_permission_prompts(
    project: Project, claude: Harness, tmp_path: Path, no_config: bool
) -> None:
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, claude, no_config=no_config)
    assert engine.execs[-1].args[-4:] == (
        "claude",
        "--dangerously-skip-permissions",
        "--append-system-prompt-file",
        "/home/agent/.devbox/environment.md",
    )


def test_seed_drops_host_specific_settings(tmp_path: Path, claude: Harness) -> None:
    claude.seed_dir.mkdir()
    (claude.seed_dir / "settings.json").write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"Stop": []},
                "statusLine": {"command": "~/bin/status"},
                "apiKeyHelper": "/usr/local/bin/key",
                "permissions": {"allow": ["Bash(ls)"], "additionalDirectories": ["/Users/a"]},
            }
        )
    )
    (claude.seed_dir / "CLAUDE.md").write_text("instructions")
    (claude.seed_dir / ".claude.json").write_text('{"mcpServers": {"host": {}}}')
    home = tmp_path / "home"
    messages = seed_config(claude, home)
    assert "omitted host-specific setting hooks" in messages
    assert "omitted host-specific setting permissions.additionalDirectories" in messages
    settings = json.loads((home / ".claude/settings.json").read_text())
    assert settings == {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}
    assert (home / ".claude/CLAUDE.md").read_text() == "instructions"
    # The host's global config (projects, MCP servers) is never copied.
    assert json.loads((home / ".claude/.claude.json").read_text()) == {
        "hasCompletedOnboarding": True
    }


def test_invalid_settings_fail(tmp_path: Path, claude: Harness) -> None:
    claude.seed_dir.mkdir()
    (claude.seed_dir / "settings.json").write_text("[]")
    with pytest.raises(DevboxError, match="settings.json is invalid"):
        seed_config(claude, tmp_path / "home")


def test_sessions_exclude_subagent_transcripts(tmp_path: Path, claude: Harness) -> None:
    store = Store(tmp_path)
    id, directory = store.create()
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "harness": "claude",
                "project": "/project",
                "session_dir": claude.session_dir,
                "session_glob": claude.session_glob,
            }
        )
    )
    project = directory / "home/.claude/projects/-project"
    (project / "abc/subagents").mkdir(parents=True)
    (project / "abc.jsonl").write_text('{"type":"last-prompt","sessionId":"abc"}\n')
    (project / "abc/subagents/agent-1.jsonl").write_text('{"sessionId":"abc"}\n')
    assert [session.id for session in store.sessions()] == [f"{id}/abc"]


def test_resume_places_snapshot_by_id_and_forks(
    project: Project, claude: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    original_id, original = app.store.create()
    (original / "metadata.json").write_text("{}")
    source = original / "home/source.jsonl"
    source.write_text('{"sessionId":"abc"}\n')
    session = Session(f"{original_id}/abc", original_id, "claude", str(project.cwd), source)
    id = app.new(project, claude, no_config=True, session=session)
    home = app.store.directory(id) / "home"
    key = "".join(c if c.isalnum() else "-" for c in str(project.workdir))
    assert (home / f".claude/projects/{key}/abc.jsonl").read_text() == source.read_text()
    assert "--dangerously-skip-permissions" in engine.execs[-1].args
    assert engine.execs[-1].args[-3:] == ("--resume", "abc", "--fork-session")


def test_claude_never_copies_the_host_login() -> None:
    assert ".credentials.json" not in Harness.load(ASSETS / "harnesses/claude").seed_files


def test_instances_share_a_devbox_only_login(
    project: Project, claude: Harness, tmp_path: Path
) -> None:
    claude.seed_dir.mkdir()
    (claude.seed_dir / ".credentials.json").write_text('"host refresh token"')
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    for _ in range(2):
        app.new(project, claude)
    shared = shared_config(claude)["auth"]
    assert shared == tmp_path / "config/devbox/shared/claude/auth"
    assert list(shared.iterdir()) == []
    for run in engine.runs:
        assert run.binds[-1] == (str(shared), "/home/agent/.devbox/claude-auth")
        # Claude's refresh locks sit inside and beside the store: both must be on the mount.
        store = PurePosixPath(run.env["CLAUDE_SECURESTORAGE_CONFIG_DIR"])
        assert store.parent == PurePosixPath("/home/agent/.devbox/claude-auth")


def test_no_config_keeps_a_private_login(project: Project, claude: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, claude, no_config=True)
    assert all("claude-auth" not in target for _, target in engine.runs[0].binds)
    assert not (tmp_path / "config/devbox/shared").exists()


@pytest.mark.parametrize("no_config", [False, True])
def test_new_trusts_project_and_skips_bypass_warning(
    project: Project, claude: Harness, tmp_path: Path, no_config: bool
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    claude.seed_dir.mkdir()
    (claude.seed_dir / "settings.json").write_text('{"model": "opus"}')
    id = app.new(project, claude, no_config=no_config)
    state = app.store.directory(id) / "home/.claude"
    config = json.loads((state / ".claude.json").read_text())
    assert config["projects"][str(project.workdir)]["hasTrustDialogAccepted"] is True
    settings = json.loads((state / "settings.json").read_text())
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings.get("model") == (None if no_config else "opus")
