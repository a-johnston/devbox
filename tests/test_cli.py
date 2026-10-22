import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_app import FakeEngine

from devbox.app import App
from devbox.cli import dispatch, get_harness, main, match, parser, select, session_label
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.state import Session, Store


@pytest.mark.parametrize("args", [["--help"], ["--version"], ["completion", "bash"]])
def test_installed_devbox_entry_point(args: list[str]) -> None:
    result = subprocess.run(
        [str(Path(sys.executable).with_name("devbox")), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()
    if args != ["--version"]:
        assert "devbox" in result.stdout


def test_new_defaults_to_current_project() -> None:
    args = parser().parse_args(["new", "pi", "--detach"])
    assert args.project == Path(".")
    assert args.harness == "pi"
    assert args.detach


@pytest.mark.parametrize("action", ["new", "build", "resume"])
def test_preparation_options(action: str) -> None:
    assert parser().parse_args([action, "--no-config"]).no_config


def test_unknown_command_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(["not-a-command"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_script_does_not_create_state(
    shell: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("completion must not initialize the application or run subprocesses")

    monkeypatch.setattr("devbox.cli.App", unexpected)
    monkeypatch.setattr("devbox.cli.subprocess.run", unexpected)
    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "completion", shell]) == 0
    assert "devbox" in capsys.readouterr().out
    assert not state.exists()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("devbox n", "new"),
        ("devbox re", "resume"),
        ("devbox cl", "cleanup"),
        ("devbox cleanup --", "--select"),
        ("devbox ", "completion"),
        ("devbox --st", "--state-dir"),
        ("devbox new --", "--detach"),
        ("devbox completion ", "fish"),
    ],
)
@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_completion(line: str, expected: str, tmp_path: Path, shell: str) -> None:
    assert expected in complete(line, tmp_path, shell=shell)


def test_completion_does_not_filter_prefix_like_filenames(tmp_path: Path) -> None:
    (tmp_path / "n").touch()
    assert "n" in complete("devbox --state-dir ", tmp_path)


def complete(line: str, tmp_path: Path, *, shell: str = "bash") -> list[str]:
    # Exercise the real entry point in a subprocess: argcomplete owns descriptors
    # 8/9 and exits with os._exit(), which must not interfere with pytest's capture.
    output = tmp_path / "completions"
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-m", "devbox.cli"],
        cwd=tmp_path,
        env={
            **os.environ,
            "_ARGCOMPLETE": "1",
            "_ARGCOMPLETE_SHELL": shell,
            "COMP_LINE": line,
            "COMP_POINT": str(len(line)),
            "_ARGCOMPLETE_STDOUT_FILENAME": str(output),
            "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
            "XDG_STATE_HOME": str(state),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not state.exists()
    values = output.read_text().split("\v")
    # Zsh receives command:description pairs; compare only the inserted text.
    return [value.partition(":")[0] for value in values] if shell == "zsh" else values


def test_prefix_selection() -> None:
    assert match(["devbox-aabb", "devbox-bbcc"], "aab", "Containers", str, str) == "devbox-aabb"
    with pytest.raises(DevboxError, match="matches 2"):
        match(["aa", "ab"], "a", "Containers", str, str)


def test_missing_argument_without_terminal_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(DevboxError, match="terminal"):
        select(["pi"], "Harnesses", str)


def test_picker_uses_fzf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: "/bin/fzf")
    monkeypatch.setattr(
        "devbox.cli.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "1\tsecond\n", ""),
    )
    assert select(["first", "second"], "Harnesses", str) == "second"


def test_session_listing_and_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = Session("devbox-aabb/conversation", "devbox-aabb", "pi", "/project", tmp_path)
    expected = "devbox-aabb/conversation  pi  /project"
    assert session_label(session) == expected
    app = App(FakeEngine(), Store(tmp_path))
    monkeypatch.setattr(app.store, "sessions", lambda: [session])
    dispatch(parser().parse_args(["sessions"]), app)
    assert capsys.readouterr().out == expected + "\n"
    dispatch(parser().parse_args(["sessions", "--json"]), app)
    (row,) = json.loads(capsys.readouterr().out)
    assert "title" not in row
    assert row["id"] == session.id
    assert row["project"] == "/project"

    monkeypatch.setattr("devbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: "/bin/fzf")

    def pick(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["input"] == f"0\t{expected}\n"
        return subprocess.CompletedProcess(args, 0, f"0\t{expected}\n", "")

    monkeypatch.setattr("devbox.cli.subprocess.run", pick)

    assert match([session], None, "Sessions", lambda s: s.id, session_label) == session
    assert (
        match([session], "aabb/conversation", "Sessions", lambda s: s.id, session_label) == session
    )


def test_picker_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: "/bin/fzf")
    monkeypatch.setattr(
        "devbox.cli.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 130, "", "")
    )
    with pytest.raises(KeyboardInterrupt):
        select(["pi"], "Harnesses", str)


def test_unknown_harness() -> None:
    with pytest.raises(DevboxError, match="Unknown harness"):
        get_harness("not-installed")


def test_stop_explicit_id_needs_no_picker(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [Container("devbox-aabb", "running", "project", "pi", "now")]
    app = App(engine, Store(tmp_path))
    dispatch(argparse.Namespace(action="stop", id="aab"), app)
    assert engine.calls == [("stop", "devbox-aabb")]


def test_kill_all_noninteractive_requires_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    engine.rows = [Container("ours", "running", "project", "pi", "now")]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(DevboxError, match="terminal"):
        dispatch(argparse.Namespace(action="kill-all", yes=False), App(engine, Store(tmp_path)))
    assert not engine.calls


def test_cli_text_is_platform_neutral() -> None:
    text = parser().format_help()
    assert "Apple" not in text
    assert "Coding harnesses in disposable containers" in text


def test_missing_fzf_hint_is_platform_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: None)
    with pytest.raises(DevboxError, match="requires fzf") as error:
        select(["first"], "Harnesses", str)
    assert "brew" not in str(error.value)
