import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_app import FakeEngine

from agentbox.app import App
from agentbox.cli import dispatch, get_harness, main, match, parser, select
from agentbox.engine import Container
from agentbox.errors import AgentboxError
from agentbox.state import Store


def test_new_defaults_to_current_project() -> None:
    args = parser().parse_args(["new", "pi", "--detach"])
    assert args.project == Path(".")
    assert args.harness == "pi"
    assert args.detach


@pytest.mark.parametrize("action", ["new", "build", "resume"])
def test_preparation_options(action: str) -> None:
    args = parser().parse_args([action, "--keep-builder", "--no-config"])
    assert args.keep_builder
    assert args.no_config


@pytest.mark.parametrize(
    ("prefix", "action"),
    [
        ("n", "new"),
        ("b", "build"),
        ("a", "attach"),
        ("sh", "shell"),
        ("st", "stop"),
        ("rm", "rm"),
        ("l", "ls"),
        ("se", "sessions"),
        ("re", "resume"),
        ("k", "kill-all"),
        ("c", "completion"),
    ],
)
def test_unique_command_prefixes(prefix: str, action: str) -> None:
    extra = ["bash"] if action == "completion" else []
    assert parser().parse_args([prefix, *extra]).action == action


@pytest.mark.parametrize("prefix", ["r", "s"])
def test_ambiguous_command_prefixes_fail(prefix: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        parser().parse_args([prefix])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_command_prefix_does_not_change_option_values_or_ids() -> None:
    args = parser().parse_args(["--state-dir", "new", "re", "n", "--project", "build"])
    assert args.state_dir == Path("new")
    assert args.action == "resume"
    assert args.session == "n"
    assert args.project == Path("build")


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

    monkeypatch.setattr("agentbox.cli.App", unexpected)
    monkeypatch.setattr("agentbox.cli.subprocess.run", unexpected)
    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "completion", shell]) == 0
    assert "agentbox" in capsys.readouterr().out
    assert not state.exists()


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("agentbox n", "new"),
        ("agentbox ", "completion"),
        ("agentbox --st", "--state-dir"),
        ("agentbox new --", "--detach"),
        ("agentbox n --", "--detach"),
        ("agentbox c ", "fish"),
    ],
)
@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_completion(line: str, expected: str, tmp_path: Path, shell: str) -> None:
    assert expected in complete(line, tmp_path, shell=shell)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("n", {"new"}),
        ("r", {"rm", "resume"}),
        ("s", {"shell", "stop", "sessions"}),
        ("co", {"completion"}),
        (
            "",
            {
                "new",
                "build",
                "attach",
                "shell",
                "stop",
                "rm",
                "ls",
                "sessions",
                "resume",
                "kill-all",
                "completion",
            },
        ),
    ],
)
def test_command_completion_omits_prefix_aliases(
    prefix: str, expected: set[str], shell: str, tmp_path: Path
) -> None:
    completions = complete(f"agentbox {prefix}", tmp_path, shell=shell)
    assert {value for value in completions if not value.startswith("-")} == expected


def test_completion_does_not_filter_prefix_like_filenames(tmp_path: Path) -> None:
    (tmp_path / "n").touch()
    assert "n" in complete("agentbox --state-dir ", tmp_path)


def complete(line: str, tmp_path: Path, *, shell: str = "bash") -> list[str]:
    # Exercise the real entry point in a subprocess: argcomplete owns descriptors
    # 8/9 and exits with os._exit(), which must not interfere with pytest's capture.
    output = tmp_path / "completions"
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-m", "agentbox.cli"],
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
    assert (
        match(["agentbox-aabb", "agentbox-bbcc"], "aab", "Containers", str, str) == "agentbox-aabb"
    )
    with pytest.raises(AgentboxError, match="matches 2"):
        match(["aa", "ab"], "a", "Containers", str, str)


def test_missing_argument_without_terminal_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(AgentboxError, match="terminal"):
        select(["pi"], "Harnesses", str)


def test_picker_uses_fzf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("agentbox.cli.shutil.which", lambda _: "/bin/fzf")
    monkeypatch.setattr(
        "agentbox.cli.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, "1\tsecond\n", ""),
    )
    assert select(["first", "second"], "Harnesses", str) == "second"


def test_picker_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("agentbox.cli.shutil.which", lambda _: "/bin/fzf")
    monkeypatch.setattr(
        "agentbox.cli.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 130, "", "")
    )
    with pytest.raises(KeyboardInterrupt):
        select(["pi"], "Harnesses", str)


def test_unknown_harness() -> None:
    with pytest.raises(AgentboxError, match="Unknown harness"):
        get_harness("not-installed")


def test_stop_explicit_id_needs_no_picker(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [Container("agentbox-aabb", "running", "project", "pi", "now")]
    app = App(engine, Store(tmp_path))
    dispatch(argparse.Namespace(action="stop", id="aab"), app)
    assert engine.calls == [("stop", "agentbox-aabb")]


def test_kill_all_noninteractive_requires_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    engine.rows = [Container("ours", "running", "project", "pi", "now")]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(AgentboxError, match="terminal"):
        dispatch(argparse.Namespace(action="kill-all", yes=False), App(engine, Store(tmp_path)))
    assert not engine.calls
