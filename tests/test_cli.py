import json
import os
import subprocess
import sys
from dataclasses import replace
from functools import partial
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from devbox import cli
from devbox.app import App
from devbox.cli import dispatch, get_harness, main, match, parser, query, select, session_label
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.engine.engine import backend_name
from devbox.errors import DevboxError
from devbox.repository import Fetched, Git
from devbox.state import Session, Store
from tests.fakes import FakeEngine, FakeRepository
from tests.test_utils import patch_input


@pytest.mark.parametrize(
    "args",
    [
        ("y", True),
        (" Y ", True),
        ("n", False),
        ("yes", False),
    ],
)
def test_query(monkeypatch: pytest.MonkeyPatch, args) -> None:
    value, expected = args
    patch_input(monkeypatch, value)
    assert expected == query("")


def test_installed_devbox_entry_point() -> None:
    # One packaging smoke test; parser behavior is covered in-process below.
    result = subprocess.run(
        [str(Path(sys.executable).with_name("devbox")), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "devbox" in result.stdout


def test_new_defaults_to_current_project() -> None:
    args = parser().parse_args(["new", "pi", "--detach"])
    assert args.project == Path(".")
    assert args.harness == "pi"
    assert args.detach


@pytest.mark.parametrize("action", ["attach", "shell"])
def test_single_container_commands_reject_multiple_ids(action: str) -> None:
    with pytest.raises(SystemExit) as exc:
        parser().parse_args([action, "first", "second"])
    assert exc.value.code == 2


def test_dispatch_initializes_app_with_custom_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = App(FakeEngine(), Store(tmp_path))
    factory = Mock(return_value=app)
    monkeypatch.setattr(cli, "App", factory)
    handler = Mock()
    args = parser().parse_args(["--state-dir", str(tmp_path), "rm", "first", "second"])
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
    dispatch(parser().parse_args(["build", "pi", "--project", str(project.cwd), "--no-config"]), app)
    build.assert_called_once_with(project, harness, no_config=True)
    assert capsys.readouterr().out == "prepared-image\n"


@pytest.mark.parametrize(("detach", "override"), [(False, False), (True, True)])
def test_fork_dispatch(
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
    argv = ["fork", "aabb/conversation", "--no-config"]
    if override:
        argv.extend(["--project", str(project.cwd)])
    if detach:
        argv.append("--detach")
    dispatch(parser().parse_args(argv), app)
    create.assert_called_once_with(project, harness, no_config=True, session=session, from_instance=False)
    assert terminal.call_count == (0 if detach else 1)
    assert attach.call_args_list == ([] if detach else [((container,), {})])


@pytest.mark.parametrize(
    ("flag", "has_clone", "expected"),
    [
        ([], True, True),
        ([], False, False),
        (["--from", "host"], True, False),
        (["--from", "instance"], True, True),
        (["--from", "instance"], False, None),
    ],
)
def test_fork_clone_source(
    flag: list[str],
    has_clone: bool,
    expected: bool | None,
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    session = Session("devbox-aabb/c", "devbox-aabb", "pi", str(project.cwd), tmp_path)
    monkeypatch.setattr(app.store, "sessions", lambda: [session])
    monkeypatch.setattr(app.store, "clones", lambda: ["devbox-aabb"] if has_clone else [])
    monkeypatch.setattr(cli, "get_harness", lambda name: harness)
    create = Mock(return_value="devbox-ccdd")
    monkeypatch.setattr(app, "new", create)
    args = parser().parse_args(["fork", "aabb/c", "--detach", *flag])
    if expected is None:
        with pytest.raises(DevboxError, match="--from host"):
            dispatch(args, app)
        return
    dispatch(args, app)
    assert create.call_args.kwargs["from_instance"] is expected


def test_multiple_container_ids_are_resolved_before_mutation(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [
        Container("devbox-aabb", "running", "project", "pi", "now"),
        Container("devbox-ccdd", "running", "project", "pi", "now"),
    ]
    app = App(engine, Store(tmp_path))
    with pytest.raises(DevboxError, match="matches 0"):
        dispatch(parser().parse_args(["rm", "aab", "missing"]), app)
    assert not engine.removed
    dispatch(parser().parse_args(["rm", "aab", "ccd", "devbox-aabb"]), app)
    assert engine.removed == [
        "devbox-aabb",
        "devbox-ccdd",
    ]


@pytest.mark.parametrize("cancel", [False, True])
def test_missing_ids_use_multi_select(cancel: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    running = Container("devbox-aabb", "running", "project", "pi", "now")
    stopped = Container("devbox-ccdd", "stopped", "project", "pi", "now")
    engine.rows = [running, stopped]
    eligible = engine.rows
    picker = Mock(return_value=[] if cancel else eligible)
    monkeypatch.setattr(cli, "select_many", picker)
    dispatch(parser().parse_args(["rm"]), App(engine, Store(tmp_path)))
    picker.assert_called_once_with(eligible, "Containers", cli.container_label)
    assert engine.removed == ([] if cancel else [c.id for c in eligible])


def test_rm_accepts_stopped_containers(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.rows = [Container("devbox-aabb", "stopped", "project", "pi", "now")]
    dispatch(parser().parse_args(["rm", "aabb"]), App(engine, Store(tmp_path)))
    assert engine.removed == ["devbox-aabb"]


@pytest.mark.parametrize("action", ["stop", "resume"])
def test_removed_commands_are_rejected(action: str) -> None:
    with pytest.raises(SystemExit):
        parser().parse_args([action])


def test_attach_stopped_container_explains_runtime_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    engine.rows = [Container("devbox-aabb", "stopped", "project", "pi", "now")]
    monkeypatch.setattr(cli, "require_terminal", Mock())
    with pytest.raises(DevboxError, match="start it with the container runtime"):
        dispatch(parser().parse_args(["attach", "aabb"]), App(engine, Store(tmp_path)))
    assert not engine.execs and not engine.connections and not engine.runs


def test_attach_removed_instance_suggests_fork(tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path))
    id, directory = app.store.create()
    (directory / "metadata.json").write_text("{}")
    with pytest.raises(DevboxError, match="removed; use devbox fork"):
        dispatch(parser().parse_args(["attach", id]), app)


@pytest.mark.parametrize("action", ["attach", "shell"])
def test_single_container_dispatch(action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    running = Container("devbox-aabb", "running", "project", "pi", "now")
    engine.rows = [running, Container("devbox-aacc", "stopped", "project", "pi", "now")]
    app = App(engine, Store(tmp_path))
    handler = Mock()
    terminal = Mock()
    monkeypatch.setattr(app, action, handler)
    monkeypatch.setattr(cli, "require_terminal", terminal)
    dispatch(parser().parse_args([action, "aabb"]), app)
    handler.assert_called_once_with(running)
    terminal.assert_called_once_with()


@pytest.mark.parametrize("json_output", [False, True])
def test_container_listing_dispatch(json_output: bool, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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


def test_sync_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    container = Container("devbox-aabb", "running", "project", "pi", "now")
    engine.rows = [container]
    app = App(engine, Store(tmp_path))
    sync = Mock(return_value=["host/main", "host/other"])
    monkeypatch.setattr(app, "sync", sync)
    dispatch(parser().parse_args(["sync", "aabb", "main", "other"]), app)
    sync.assert_called_once_with(container, ["main", "other"])


def test_kill_all_yes_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    engine.rows = [Container("devbox-aabb", "running", "project", "pi", "now")]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    dispatch(parser().parse_args(["rm-all", "--yes"]), App(engine, Store(tmp_path)))
    assert engine.removed == ["devbox-aabb"]


@pytest.fixture
def fetching(project: Project, harness: Harness, tmp_path: Path) -> tuple[App, str, FakeRepository]:
    """An instance whose fetch finds main and one changed branch."""
    repository = FakeRepository()
    app = App(FakeEngine(), Store(tmp_path / "state"), repository)
    id = app.new(project, harness, no_config=True)
    short = id.removeprefix("devbox-")
    repository.fetched = [
        Fetched(f"refs/devbox/{id}/main", 0, False, (), None, False),
        Fetched(f"refs/devbox/{id}/feature/x", 1, False, (".devbox/Containerfile",), f"{short}/feature/x", True),
    ]
    return app, id, repository


def diffs(repository: FakeRepository) -> list[tuple[object, ...]]:
    return [call[1:] for call in repository.calls if call[0] == "show_diff"]


def test_fetch_dispatch_prints_flagged_refs(
    fetching: tuple[App, str, FakeRepository], capsys: pytest.CaptureFixture[str]
) -> None:
    app, id, _ = fetching
    capsys.readouterr()
    dispatch(parser().parse_args(["fetch", id.removeprefix("devbox-")[:6]]), app)
    out = capsys.readouterr().out
    assert f"refs/devbox/{id}/feature/x  1 new commit  CHANGES .devbox/Containerfile" in out
    assert f"refs/devbox/{id}/main  0 new commits\n" in out
    assert f"local branch {id.removeprefix('devbox-')}/feature/x" in out
    assert "WARNING" not in out
    assert "Compared with the current host HEAD (merge base per branch); see devbox review." in out
    assert "git merge" not in out


def test_fetched_label_warns_about_unpublished_and_rewritten_branches() -> None:
    branch = Fetched("refs/devbox/i/feature", 2, True, (), "i/feature", False)
    assert cli.fetched_label(branch) == (
        "refs/devbox/i/feature  2 new commits  REWRITTEN since the last fetch  "
        "WARNING: local branch i/feature not updated (checked out, changed locally, or could not be created); "
        "use refs/devbox/i/feature"
    )


def test_review_diffs_the_only_changed_branch(
    fetching: tuple[App, str, FakeRepository],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app, id, repository = fetching
    monkeypatch.setattr(cli, "select", lambda *a: pytest.fail("one changed branch needs no picker"))
    dispatch(parser().parse_args(["review", id]), app)
    assert "1 new commit" in capsys.readouterr().out
    assert diffs(repository) == [(Path(app.store.metadata(id)["root"]), f"refs/devbox/{id}/feature/x")]


def test_review_matches_named_branches_and_passes_stat(fetching: tuple[App, str, FakeRepository]) -> None:
    app, id, repository = fetching
    dispatch(parser().parse_args(["review", id, "main", "--stat"]), app)
    assert diffs(repository) == [(Path(app.store.metadata(id)["root"]), f"refs/devbox/{id}/main", "--stat")]


def test_review_picks_among_changed_branches(
    fetching: tuple[App, str, FakeRepository], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, id, repository = fetching
    repository.fetched.append(replace(repository.fetched[1], ref=f"refs/devbox/{id}/two"))
    offered = []

    def pick(items: list, prompt: str, label: object) -> object:
        offered.extend(item.ref for item in items)
        return items[-1]

    monkeypatch.setattr(cli, "select", pick)
    dispatch(parser().parse_args(["review", id]), app)
    assert offered == [f"refs/devbox/{id}/{name}" for name in ("feature/x", "two")]
    assert diffs(repository)[0][1] == f"refs/devbox/{id}/two"


def test_show_diff_runs_git_diff_from_the_merge_base(monkeypatch: pytest.MonkeyPatch) -> None:
    execs = []
    monkeypatch.setattr("devbox.repository.os.execvpe", lambda f, argv, env: execs.append((f, argv)))
    Git().show_diff(Path("/host"), "refs/devbox/i/x", "--stat")
    assert execs == [("git", ["git", "-C", "/host", "diff", "--stat", "HEAD...refs/devbox/i/x", "--"])]


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


def test_init_creates_missing_templates_without_engine_or_state(
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("devbox.cli.App", lambda *a, **kw: pytest.fail("init needs no engine"))
    (repository / ".devbox.toml").unlink()
    (repository / "nested").mkdir()
    state = tmp_path / "state"
    assert main(["--state-dir", str(state), "init", "--project", str(repository / "nested")]) == 0
    out = capsys.readouterr().out
    assert f"Created {repository / '.devbox.toml'}" in out
    assert f"Kept existing {repository / '.devbox/Containerfile'}" in out
    assert (repository / ".devbox/Containerfile").read_text() == "FROM ubuntu:24.04\n"
    assert not state.exists()
    project = Project.discover(repository)
    assert (project.containerfile, project.context) == (
        repository / ".devbox/Containerfile",
        repository / ".devbox",
    )
    assert (project.cpus, project.memory, project.env, project.watch) == (4, "8G", {}, ())


@pytest.mark.parametrize(
    ("shell", "line", "expected"),
    [
        ("bash", "devbox n", "new"),
        ("bash", "devbox fo", "fork"),
        ("bash", "devbox cl", "cleanup"),
        ("bash", "devbox cleanup --", "--select"),
        ("bash", "devbox ", "completion"),
        ("bash", "devbox --st", "--state-dir"),
        ("bash", "devbox new --", "--detach"),
        ("bash", "devbox completion ", "fish"),
        # Shell-specific formatting needs a smoke test, not the full parser matrix.
        ("zsh", "devbox n", "new"),
        ("fish", "devbox n", "new"),
    ],
)
def test_shell_completion(
    line: str, expected: str, tmp_path: Path, shell: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert expected in complete(line, tmp_path, monkeypatch, shell=shell)


def test_completion_does_not_filter_prefix_like_filenames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "n").touch()
    assert "n" in complete("devbox --state-dir ", tmp_path, monkeypatch)


def complete(line: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, shell: str = "bash") -> list[str]:
    # Argcomplete supports injectable output/exit methods: exercise main() without
    # starting another interpreter or letting os._exit() terminate pytest.
    output = StringIO()
    finder = cli.argcomplete.CompletionFinder()
    # Do not let argcomplete take ownership of pytest's fd 9 for shell debugging.
    monkeypatch.setattr(finder, "_init_debug_stream", lambda: None)
    monkeypatch.setattr(cli.argcomplete, "autocomplete", partial(finder, output_stream=output, exit_method=sys.exit))
    monkeypatch.setattr(cli, "App", lambda *a, **kw: pytest.fail("completion needs no app"))
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "state"
    for key, value in {
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_SHELL": shell,
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "XDG_STATE_HOME": str(state),
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert not state.exists()
    values = output.getvalue().split("\v")
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
    assert match([session], "aabb/conversation", "Sessions", lambda s: s.id, session_label) == session


def test_picker_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda: None)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: "/bin/fzf")
    monkeypatch.setattr("devbox.cli.subprocess.run", lambda *a, **kw: subprocess.CompletedProcess(a, 130, "", ""))
    with pytest.raises(KeyboardInterrupt):
        select(["pi"], "Harnesses", str)


def test_unknown_harness() -> None:
    with pytest.raises(DevboxError, match="Unknown harness"):
        get_harness("not-installed")


def test_kill_all_noninteractive_requires_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine()
    engine.rows = [Container("ours", "running", "project", "pi", "now")]
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(DevboxError, match="terminal"):
        dispatch(parser().parse_args(["rm-all"]), App(engine, Store(tmp_path)))
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


@pytest.fixture
def saved_instances(tmp_path: Path) -> Path:
    state = tmp_path / "saved"
    for id, clone in (("devbox-blue", True), ("devbox-1a2b3c4d", False)):
        directory = state / backend_name() / "instances" / id
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text("{}")
        if clone:
            (directory / "repo/.git").mkdir(parents=True)
    return state


@pytest.mark.parametrize(
    ("line", "expected", "absent"),
    [
        ("devbox attach ", {"blue", "1a2b3c4d"}, {"devbox-blue"}),
        ("devbox attach b", {"blue"}, {"1a2b3c4d"}),
        ("devbox attach devbox-b", {"devbox-blue"}, {"blue"}),
        ("devbox rm blue ", {"1a2b3c4d"}, set()),
        ("devbox fetch ", {"blue"}, {"1a2b3c4d"}),
        ("devbox review ", {"blue"}, {"1a2b3c4d"}),
    ],
)
def test_instance_id_completion(
    line: str,
    expected: set[str],
    absent: set[str],
    saved_instances: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = complete(
        f"devbox --state-dir {saved_instances} {line.removeprefix('devbox ')}",
        tmp_path,
        monkeypatch,
    )
    assert expected <= set(found)
    assert not absent & set(found)


def test_instance_id_completion_without_saved_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = complete(f"devbox --state-dir {tmp_path / 'nope'} attach ", tmp_path, monkeypatch)
    assert not [c for c in found if not c.startswith("-")]


def test_main_restores_umask(tmp_path: Path) -> None:
    before = os.umask(0o022)
    try:
        assert main(["--state-dir", str(tmp_path / "state"), "completion", "bash"]) == 0
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(before)
