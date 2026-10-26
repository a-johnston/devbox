from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App, environment_secrets
from devbox.cli import parser
from devbox.config import ASSETS, Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine, FakeGit


@pytest.fixture
def secretive(harness: Harness) -> Harness:
    return replace(harness, secrets=("HARNESS_TOKEN",))


@pytest.fixture
def secrets_config(tmp_path: Path) -> Path:
    path = tmp_path / "config/devbox/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("")
    return path


@pytest.mark.parametrize("source", ["environment", "config"])
def test_secrets_reach_only_the_harness_process(
    project: Project,
    secretive: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_config: Path,
    source: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    if source == "environment":
        monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    else:
        secrets_config.write_text('[secrets]\nHARNESS_TOKEN = "s3cret"\n')
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
    id = app.new(project, secretive, no_config=False)
    startup = engine.execs[-1]
    assert startup.id == id and startup.args[:3] == (
        "bash",
        "/home/agent/.devbox/harness.sh",
        "start",
    )
    assert startup.env == {"HARNESS_TOKEN": "s3cret"}
    assert all(request.env is None for request in engine.execs[:-1])
    assert "s3cret" not in str(engine.builds) + str(engine.runs)
    assert all("s3cret" not in str(request.args) for request in engine.execs)
    assert "s3cret" not in capsys.readouterr().out
    home = app.store.directory(id)
    assert not any(b"s3cret" in p.read_bytes() for p in home.rglob("*") if p.is_file())


@pytest.mark.parametrize("content", ['[secrets]\nHARNESS_TOKEN = "from-file"', 'invalid = "'])
def test_no_config_skips_secrets(
    project: Project,
    secretive: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secrets_config: Path,
    content: str,
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    secrets_config.write_text(content)
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state"), FakeGit()).new(project, secretive, no_config=True)
    assert engine.execs[-1].env == {}


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_secrets_are_skipped(
    secretive: Harness, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("HARNESS_TOKEN", value)
    assert environment_secrets(secretive.secrets) == {}


def test_only_declared_secrets_are_forwarded(secretive: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "s3cret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "other")
    assert environment_secrets(secretive.secrets) == {"HARNESS_TOKEN": "s3cret"}


@pytest.mark.parametrize("value", [None, "", "override"])
def test_environment_overrides_config_secrets(
    secretive: Harness,
    secrets_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    secrets_config.write_text('[secrets]\nHARNESS_TOKEN = "from-file"\n')
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("HARNESS_TOKEN", value)
    assert environment_secrets(secretive.secrets) == {"HARNESS_TOKEN": value or "from-file"}


def test_only_declared_config_secrets_are_forwarded(
    secretive: Harness, secrets_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    secrets_config.write_text('[secrets]\nHARNESS_TOKEN = "s3cret"\nAWS_SECRET_ACCESS_KEY = "other"\n')
    assert environment_secrets(secretive.secrets) == {"HARNESS_TOKEN": "s3cret"}


@pytest.mark.parametrize("content", ["", '[secrets]\nHARNESS_TOKEN = ""', "[unrelated]"])
def test_missing_or_empty_config_secrets_are_skipped(
    secretive: Harness,
    secrets_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    secrets_config.write_text(content)
    assert environment_secrets(secretive.secrets) == {}


@pytest.mark.parametrize("xdg", [None, "", "custom", "~/custom"])
def test_config_location(secretive: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if xdg is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / xdg) if xdg == "custom" else xdg)
    base = tmp_path / ("custom" if xdg else ".config")
    path = base / "devbox/config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[secrets]\nHARNESS_TOKEN = "from-file"\n')
    assert environment_secrets(secretive.secrets) == {"HARNESS_TOKEN": "from-file"}


@pytest.mark.parametrize(
    "content",
    [
        '[secrets]\nHARNESS_TOKEN = "s3cret" trailing',
        'secrets = "s3cret"',
        '[secrets]\nHARNESS_TOKEN = ["s3cret"]',
        '[secrets]\n"not valid" = "s3cret"',
    ],
)
def test_invalid_secrets_config_fails_without_revealing_values(
    secretive: Harness, secrets_config: Path, content: str
) -> None:
    secrets_config.write_text(content)
    with pytest.raises(DevboxError) as error:
        environment_secrets(secretive.secrets)
    assert str(secrets_config) in str(error.value)
    assert "s3cret" not in str(error.value)


def test_invalid_config_encoding_fails(secretive: Harness, secrets_config: Path) -> None:
    secrets_config.write_bytes(b'[secrets]\nHARNESS_TOKEN = "s3cret\xff"')
    with pytest.raises(DevboxError, match="Cannot read") as error:
        environment_secrets(secretive.secrets)
    assert "s3cret" not in str(error.value)


def test_unreadable_secrets_config_fails(secretive: Harness, secrets_config: Path) -> None:
    secrets_config.unlink()
    secrets_config.mkdir()
    with pytest.raises(DevboxError, match="Cannot read"):
        environment_secrets(secretive.secrets)


def test_harness_without_secrets_does_not_read_config(harness: Harness, secrets_config: Path) -> None:
    secrets_config.write_text('invalid = "')
    assert environment_secrets(harness.secrets) == {}


def test_claude_reads_its_oauth_token_from_config(secrets_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    secrets_config.write_text('[secrets]\nCLAUDE_CODE_OAUTH_TOKEN = "claude-token"\n')
    assert environment_secrets(Harness.load(ASSETS / "harnesses/claude").secrets) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-token"
    }


def test_secret_command_is_gone() -> None:
    with pytest.raises(SystemExit):
        parser().parse_args(["secret", "claude"])


def test_invalid_secret_names(tmp_path: Path, harness: Harness) -> None:
    text = (harness.directory / "profile.toml").read_text()
    (tmp_path / "profile.toml").write_text(text.replace("[environment]", 'secrets = ["not valid"]\n\n[environment]'))
    (tmp_path / "Containerfile").write_text("")
    with pytest.raises(DevboxError, match="environment"):
        Harness.load(tmp_path)


def test_claude_declares_its_oauth_token() -> None:
    assert Harness.load(ASSETS / "harnesses/claude").secrets == ("CLAUDE_CODE_OAUTH_TOKEN",)
