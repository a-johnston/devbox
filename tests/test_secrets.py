from dataclasses import replace
from pathlib import Path

import pytest
from test_app import FakeEngine

from agentbox.app import App, environment_secrets
from agentbox.cli import parser
from agentbox.config import ASSETS, Harness, Project
from agentbox.errors import AgentboxError
from agentbox.state import Store


@pytest.fixture
def secretive(harness: Harness) -> Harness:
    return replace(harness, secrets=("HARNESS_TOKEN",))


def test_secrets_reach_only_the_harness_process(
    project: Project, secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, secretive, no_config=False)
    startup = engine.calls[-1]
    assert startup[:5] == ("exec", "--user", "agent", "--env", "HARNESS_TOKEN")
    assert engine.env == {"HARNESS_TOKEN": "s3cret"}
    assert not any("s3cret" in arg for call in engine.calls for arg in call)
    home = app.store.directory(id)
    assert not any("s3cret" in p.read_text() for p in home.rglob("*") if p.is_file())


def test_no_config_skips_secrets(
    project: Project, secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, secretive, no_config=True)
    assert "--env" not in engine.calls[-1][:5]


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_secrets_are_skipped(
    secretive: Harness, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HARNESS_TOKEN", value)
    assert environment_secrets(secretive) == {}


def test_only_declared_secrets_are_forwarded(
    secretive: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other")
    assert environment_secrets(secretive) == {"HARNESS_TOKEN": "s3cret"}


def test_secret_command_is_gone() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(["secret", "claude"])


def test_invalid_secret_names(tmp_path: Path, harness: Harness) -> None:
    text = (harness.directory / "profile.toml").read_text()
    (tmp_path / "profile.toml").write_text(
        text.replace("[environment]", 'secrets = ["not valid"]\n\n[environment]')
    )
    (tmp_path / "Containerfile").write_text("")
    with pytest.raises(AgentboxError, match="environment"):
        Harness.load(tmp_path)


def test_claude_declares_its_oauth_token() -> None:
    assert Harness.load(ASSETS / "harnesses/claude").secrets == ("CLAUDE_CODE_OAUTH_TOKEN",)
