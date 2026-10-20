import argparse
import subprocess
from pathlib import Path

import pytest
from test_app import FakeEngine

from agentbox.app import App
from agentbox.cli import dispatch, get_harness, match, parser, select
from agentbox.engine import Container
from agentbox.errors import AgentboxError
from agentbox.state import Store


def test_new_defaults_to_current_project() -> None:
    args = parser().parse_args(["new", "pi", "--detach"])
    assert args.project == Path(".")
    assert args.harness == "pi"
    assert args.detach


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
