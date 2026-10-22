import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from devbox import cli
from devbox.app import App
from devbox.cli import dispatch, get_harness, main, match, parser, query, select, session_label
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.state import Session, Store
from tests.fakes import FakeEngine
from tests.test_utils import patch_input


@pytest.mark.parametrize(
    "args",
    [
        ("y", True),
        ("Y", True),
        (" Y ", True),
        ("n", False),
        ("No", False),
        ("asd", False),
        ("yes", False),
    ],
)
def test_query(monkeypatch: pytest.MonkeyPatch, args) -> None:
    value, expected = args
    patch_input(monkeypatch, value)
    assert expected == query("")


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


@pytest.mark.parametrize(
    ("argv", "handler", "keys"),
    [
        (["new"], cli._run, {"harness", "project", "no_config", "detach"}),
        (["build"], cli._build, {"harness", "project", "no_config"}),
        (["attach"], cli._attach, {"id"}),
        (["shell"], cli._shell, {"id"}),
        (["stop"], cli._stop, {"ids"}),
        (["rm"], cli._rm, {"ids"}),
        (["ls"], cli._ls, {"json_output"}),
        (["sessions"], cli._sessions, {"json_output"}),
        (["resume"], cli._resume, {"session", "project", "detach", "no_config"}),
        (["kill-all"], cli._kill_all, {"yes"}),
        (["cleanup"], cli.cleanup, {"select"}),
        (["completion", "bash"], cli._completion, {"shell"}),
    ],
)
def test_parser_selects_handler_with_only_its_arguments(argv, handler, keys) -> None:
    values = vars(parser().parse_args(argv))
    assert values.pop("func") is handler
    assert values.pop("state_dir") is None
    assert set(values) == keys
    if handler is not cli._completion:
        values["app"] = object()
    inspect.signature(handler).bind(**values)


@pytest.mark.parametrize("action", ["attach", "shell"])
def test_single_container_commands_reject_multiple_ids(action: str) -> None:
    with pytest.raises(SystemExit) as exc:
        parser().parse_args([action, "first", "second"])
    assert exc.value.code == 2


def test_dispatch_initializes_app_with_custom_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = App(FakeEngine(), Store(tmp_path))
    factory = Mock(return_value=app)
    monkeypatch.setattr(cli, "App", factory)
    handler = Mock()
    args = parser().parse_args(["--state-dir", str(tmp_path), "stop", "first", "second"])
    args.func = handler
    dispatch(args)
    assert factory.call_args.kwargs["store"].root == app.store.root
    handler.assert_called_once_with(app=app, ids=["first", "second"])
    assert "func" not in vars(args)
    assert "state_dir" not in vars(args)


@pytest.mark.parametrize("detach", [False, True])
def test_new_dispatch_creates_and_optionally_attaches(
    detach: bool,
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    container = Container("devbox-aabb", "running", "project", "pi", "now")
    engine.rows = [container]
    app = App(engine, Store(tmp_path / "state"))
    create = Mock(return_value=container.id)
    attach = Mock()
    terminal = Mock()
    monkeypatch.setattr(app, "new", create)
    monkeypatch.setattr(app, "attach", attach)
    monkeypatch.setattr(cli, "require_terminal", terminal)
    monkeypatch.setattr(cli, "get_harness", lambda name: harness)
    argv = ["new", "pi", "--project", str(project.cwd), "--no-config"]
    dispatch(parser().parse_args([*argv, *(["--detach"] if detach else [])]), app)
    create.assert_called_once_with(project, harness, no_config=True)
    assert terminal.call_count == (0 if detach else 1)
    assert attach.call_args_list == ([] if detach else [((container,), {})])


def test_build_dispatch(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    build = Mock(return_value="prepared-image")
    monkeypatch.setattr(app, "build", build)
    monkeypatch.setattr(cli, "get_harness", lambda name: harness)
    dispatch(
        parser().parse_args(["build", "pi", "--project", str(project.cwd), "--no-config"]), app
    )
    build.assert_called_once_with(project, harness, no_config=True)
    assert capsys.readouterr().out == "prepared-image\n"


@pytest.mark.parametrize("detach", [False, True])
@pytest.mark.parametrize("override", [False, True])
def test_resume_dispatch(
    detach: bool,
    override: bool,
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FakeEngine()
    container = Container("devbox-ccdd", "running", "project", "pi", "now")
    engine.rows = [container]
    app = App(engine, Store(tmp_path / "state"))
    session = Session(
        "devbox-aabb/conversation",
        "devbox-aabb",
        "pi",
        "/missing" if override else str(project.cwd),
        tmp_path,
    )
    monkeypatch.setattr(app.store, "sessions", lambda: [session])
    create = Mock(return_value=container.id)
    attach = Mock()
    terminal = Mock()
    monkeypatch.setattr(app, "new", create)
    monkeypatch.setattr(app, "attach", attach)
    monkeypatch.setattr(cli, "require_terminal", terminal)
    monkeypatch.setattr(cli, "get_harness", lambda name: harness)
    argv = ["resume", "aabb/conversation", "--no-config"]
    if override:
        argv.extend(["--project", str(project.cwd)])
    if detach:
        argv.append("--detach")
    dispatch(parser().parse_args(argv), app)
    create.assert_called_once_with(project, harness, no_config=True, session=session)
    assert terminal.call_count == (0 if detach else 1)
    assert attach.call_args_list == ([] if detach else [((container,), {})])


@pytest.mark.parametrize("action", ["stop", "rm"])
def test_multiple_container_ids_are_resolved_before_mutation(action: str, tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [
        Container("devbox-aabb", "running", "project", "pi", "now"),
        Container("devbox-ccdd", "running", "project", "pi", "now"),
    ]
    app = App(engine, Store(tmp_path))
    with pytest.raises(DevboxError, match="matches 0"):
        dispatch(parser().parse_args([action, "aab", "missing"]), app)
    assert not engine.stopped
    assert not engine.removed
    dispatch(parser().parse_args([action, "aab", "ccd", "devbox-aabb"]), app)
    assert (engine.stopped if action == "stop" else engine.removed) == [
        "devbox-aabb",
        "devbox-ccdd",
    ]


@pytest.mark.parametrize("action", ["stop", "rm"])
@pytest.mark.parametrize("cancel", [False, True])
def test_missing_ids_use_multi_select(
    action: str, cancel: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    running = Container("devbox-aabb", "running", "project", "pi", "now")
    stopped = Container("devbox-ccdd", "stopped", "project", "pi", "now")
    engine.rows = [running, stopped]
    eligible = [running] if action == "stop" else engine.rows
    picker = Mock(return_value=[] if cancel else eligible)
    monkeypatch.setattr(cli, "select_many", picker)
    dispatch(parser().parse_args([action]), App(engine, Store(tmp_path)))
    picker.assert_called_once_with(eligible, "Containers", cli.container_label)
    assert (engine.stopped if action == "stop" else engine.removed) == (
        [] if cancel else [c.id for c in eligible]
    )


@pytest.mark.parametrize("action", ["stop", "rm"])
def test_stopped_container_eligibility(action: str, tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [Container("devbox-aabb", "stopped", "project", "pi", "now")]
    args = parser().parse_args([action, "aabb"])
    app = App(engine, Store(tmp_path))
    if action == "stop":
        with pytest.raises(DevboxError, match="matches 0"):
            dispatch(args, app)
        assert not engine.stopped
    else:
        dispatch(args, app)
        assert engine.removed == ["devbox-aabb"]


@pytest.mark.parametrize("action", ["attach", "shell"])
def test_single_container_dispatch(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    running = Container("devbox-aabb", "running", "project", "pi", "now")
    engine.rows = [running, Container("devbox-aacc", "stopped", "project", "pi", "now")]
    app = App(engine, Store(tmp_path))
    handler = Mock()
    terminal = Mock()
    monkeypatch.setattr(app, action, handler)
    monkeypatch.setattr(cli, "require_terminal", terminal)
    dispatch(parser().parse_args([action, "aa"]), app)
    handler.assert_called_once_with(running)
    terminal.assert_called_once_with()


@pytest.mark.parametrize("json_output", [False, True])
def test_container_listing_dispatch(
    json_output: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = FakeEngine()
    engine.rows = [
        Container("devbox-aabb", "running", "project", "pi", "now"),
        Container("devbox-ccdd", "stopped", "project", "pi", "now"),
    ]
    dispatch(
        parser().parse_args(["ls", *(["--json"] if json_output else [])]),
        App(engine, Store(tmp_path)),
    )
    output = capsys.readouterr().out
    if json_output:
        assert [row["id"] for row in json.loads(output)] == [c.id for c in engine.rows]
    else:
        assert output.splitlines() == [cli.container_label(c) for c in engine.rows]


def test_kill_all_yes_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    engine.rows = [Container("devbox-aabb", "running", "project", "pi", "now")]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    dispatch(parser().parse_args(["kill-all", "--yes"]), App(engine, Store(tmp_path)))
    assert engine.removed == ["devbox-aabb"]


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
    dispatch(parser().parse_args(["stop", "aab"]), app)
    assert engine.stopped == ["devbox-aabb"]


def test_kill_all_noninteractive_requires_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = FakeEngine()
    engine.rows = [Container("ours", "running", "project", "pi", "now")]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(DevboxError, match="terminal"):
        dispatch(parser().parse_args(["kill-all"]), App(engine, Store(tmp_path)))
    assert not engine.removed


def test_cli_text_is_platform_neutral() -> None:
    text = parser().format_help()
    assert "Apple" not in text
    assert "Containerized coding harnesses" in text


def test_missing_fzf_hint_is_platform_neutral(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: None)
    with pytest.raises(DevboxError, match="requires fzf") as error:
        select(["first"], "Harnesses", str)
    assert "brew" not in str(error.value)
