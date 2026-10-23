from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App, environment_secrets
from devbox.cli import parser
from devbox.config import ASSETS, Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine


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
    startup = engine.execs[-1]
    assert startup.id == id and startup.args[0] == "tmux"
    assert startup.env == {"HARNESS_TOKEN": "s3cret"}
    assert all(request.env is None for request in engine.execs[:-1])
    assert "s3cret" not in str(engine.builds) + str(engine.runs)
    assert all("s3cret" not in str(request.args) for request in engine.execs)
    home = app.store.directory(id)
    assert not any(b"s3cret" in p.read_bytes() for p in home.rglob("*") if p.is_file())


def test_no_config_skips_secrets(
    project: Project, secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, secretive, no_config=True)
    assert engine.execs[-1].env == {}


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
    with pytest.raises(DevboxError, match="environment"):
        Harness.load(tmp_path)


def test_claude_declares_its_oauth_token() -> None:
    assert Harness.load(ASSETS / "harnesses/claude").secrets == ("CLAUDE_CODE_OAUTH_TOKEN",)
