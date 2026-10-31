import argparse
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

import argcomplete
import pytest

from devbox.cli_builder import CliCommands, arg


def build(commands: CliCommands) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="example")
    commands.build_subcommands(parser)
    return parser


def invoke(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    values = vars(parser.parse_args(argv))
    values.pop("func")(**values)


def test_registration_argument_order_and_callable_identity() -> None:
    commands = CliCommands()
    target = Mock()

    @commands.command("copy", help="Copy files")
    @arg("source")
    @arg("destination")
    @arg("--to", type=Path, default=Path("."))
    @arg("--force", action="store_true")
    def copy(*, source: str, destination: str, to: Path, force: bool) -> None:
        target(source=source, destination=destination, to=to, force=force)

    parser = build(commands)
    assert "Copy files" in parser.format_help()
    assert parser.parse_args(["copy", "a", "b"]).func is copy
    invoke(parser, ["copy", "a", "b"])
    target.assert_called_once_with(source="a", destination="b", to=Path("."), force=False)
    target.reset_mock()
    invoke(parser, ["copy", "a", "b", "--to", "out", "--force"])
    target.assert_called_once_with(source="a", destination="b", to=Path("out"), force=True)


def test_commands_without_arguments_and_independent_registries() -> None:
    first = CliCommands()
    second = CliCommands()
    target = Mock()
    first.command("run")(target)
    other = Mock()
    second.command("run")(other)
    invoke(build(first), ["run"])
    target.assert_called_once_with()
    other.assert_not_called()
    invoke(build(second), ["run"])
    other.assert_called_once_with()


@pytest.mark.parametrize("argv", [[], ["unknown"], ["run", "extra"]])
def test_requires_a_valid_subcommand(argv: list[str]) -> None:
    commands = CliCommands()
    commands.command("run")(Mock())
    with pytest.raises(SystemExit) as exc:
        build(commands).parse_args(argv)
    assert exc.value.code == 2


def test_duplicate_registration_is_rejected_without_corrupting_the_registry() -> None:
    commands = CliCommands()
    handler = commands.command("run")(Mock())
    with pytest.raises(ValueError, match="Duplicate command name"):
        commands.command("run")(Mock())
    with pytest.raises(ValueError, match="already registered"):
        commands.command("renamed")(handler)
    assert commands.commands == [handler]
    assert handler.name == "run"


def test_mutually_exclusive_groups_are_scoped_to_each_command() -> None:
    commands = CliCommands()
    target = Mock()
    handler = arg("--yes", action="store_true", exclusive_group="answer")(
        arg("--no", action="store_true", exclusive_group="answer")(target)
    )
    commands.command("first")(handler)
    commands.command("second")(arg("--other", action="store_true", exclusive_group="answer")(Mock()))
    parser = build(commands)
    invoke(parser, ["first"])
    target.assert_called_once_with(yes=False, no=False)
    target.reset_mock()
    invoke(parser, ["first", "--yes"])
    target.assert_called_once_with(yes=True, no=False)
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["first", "--yes", "--no"])
    assert exc.value.code == 2
    assert parser.parse_args(["second", "--other"]).other


def test_building_multiple_parsers_does_not_consume_metadata() -> None:
    commands = CliCommands()
    handler = commands.command("run")(arg("first")(arg("rest", nargs="*")(arg("-b", action="append", default=[])(Mock()))))
    first = build(commands)
    second = build(commands)
    assert vars(first.parse_args(["run", "one", "two", "-b", "main"])) == {
        "func": handler,
        "first": "one",
        "rest": ["two"],
        "b": ["main"],
    }
    assert vars(second.parse_args(["run", "one"])) == {
        "func": handler,
        "first": "one",
        "rest": [],
        "b": [],
    }


@pytest.mark.parametrize(("line", "expected"), [("example get ", "instance-id"), ("example get box ", "guest-path")])
def test_completers_belong_to_individual_arguments(line: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = CliCommands()
    id_completer = Mock(return_value=["instance-id"])
    paths_completer = Mock(return_value=["guest-path"])
    commands.command("get")(arg("id", completer=id_completer)(arg("paths", nargs="*", completer=paths_completer)(Mock())))
    finder = argcomplete.CompletionFinder()
    monkeypatch.setattr(finder, "_init_debug_stream", lambda: None)
    for key, value in {
        "_ARGCOMPLETE": "1",
        "COMP_LINE": line,
        "COMP_POINT": str(len(line)),
    }.items():
        monkeypatch.setenv(key, value)
    output = StringIO()
    with pytest.raises(SystemExit) as exc:
        finder(build(commands), output_stream=output, exit_method=sys.exit)
    assert exc.value.code == 0
    assert expected in output.getvalue().split("\v")
    selected = id_completer if expected == "instance-id" else paths_completer
    assert selected.called
    if expected == "guest-path":
        assert paths_completer.call_args.kwargs["parsed_args"].id == "box"
