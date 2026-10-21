import io
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_app import FakeEngine

from agentbox import keychain
from agentbox.app import App
from agentbox.cli import dispatch, parser
from agentbox.config import ASSETS, Harness, Project
from agentbox.errors import AgentboxError
from agentbox.state import Store


@pytest.fixture
def secretive(harness: Harness) -> Harness:
    return replace(harness, secrets=("HARNESS_TOKEN",))


def test_secrets_reach_only_the_harness_process(
    project: Project, secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(keychain, "load", lambda harness: {"HARNESS_TOKEN": "s3cret"})
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    id = app.new(project, secretive, no_config=False)
    startup = engine.calls[-1]
    assert startup[:4] == ("exec", "--user", "agent", "--env")
    assert startup[4] == "HARNESS_TOKEN"
    assert engine.env == {"HARNESS_TOKEN": "s3cret"}
    assert not any("s3cret" in arg for call in engine.calls for arg in call)
    home = app.store.directory(id)
    assert not any("s3cret" in p.read_text() for p in home.rglob("*") if p.is_file())


def test_no_config_skips_secrets(
    project: Project, secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(harness: Harness) -> dict[str, str]:
        raise AssertionError("Keychain read with --no-config")

    monkeypatch.setattr(keychain, "load", fail)
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state")).new(project, secretive, no_config=True)
    assert "--env" not in engine.calls[-1][:5]


def test_store_passes_value_on_stdin(secretive: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[tuple[list[str], Any]] = []

    def run(args: list[str], *, input: str | None = None, **kwargs: Any) -> Any:
        runs.append((args, input))
        stdout = "tok.en-1\n" if "find-generic-password" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    keychain.store(secretive, "HARNESS_TOKEN", "tok.en-1")
    (args, stdin), _ = runs
    assert args == ["security", "-i"]
    assert stdin is not None and '-a pi/HARNESS_TOKEN -w "tok.en-1"' in stdin
    assert not any("tok.en-1" in arg for args, _ in runs for arg in args)


@pytest.mark.parametrize("value", ["", 'a"b', "a b", "a\nquit"])
def test_store_rejects_values_that_could_escape_the_command(secretive: Harness, value: str) -> None:
    with pytest.raises(AgentboxError, match="token"):
        keychain.store(secretive, "HARNESS_TOKEN", value)


def test_invalid_secret_names(tmp_path: Path, harness: Harness) -> None:
    text = (harness.directory / "profile.toml").read_text()
    (tmp_path / "profile.toml").write_text(
        text.replace("[environment]", 'secrets = ["not valid"]\n\n[environment]')
    )
    (tmp_path / "Containerfile").write_text("")
    with pytest.raises(AgentboxError, match="environment"):
        Harness.load(tmp_path)


def test_secret_command_reads_stdin_without_a_terminal(
    secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = {}
    monkeypatch.setattr("agentbox.cli.get_harness", lambda name: secretive)
    monkeypatch.setattr(keychain, "store", lambda h, name, value: stored.update({name: value}))
    monkeypatch.setattr("sys.stdin", io.StringIO("tok.en-1\n"))
    dispatch(parser().parse_args(["secret", "pi"]), App(FakeEngine(), Store(tmp_path)))
    assert stored == {"HARNESS_TOKEN": "tok.en-1"}


def test_secret_command_requires_declared_secrets(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agentbox.cli.get_harness", lambda name: harness)
    with pytest.raises(AgentboxError, match="declares no secrets"):
        dispatch(parser().parse_args(["secret", "pi"]), App(FakeEngine(), Store(tmp_path)))


def test_claude_declares_its_oauth_token() -> None:
    assert Harness.load(ASSETS / "harnesses/claude").secrets == ("CLAUDE_CODE_OAUTH_TOKEN",)
