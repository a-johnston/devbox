from argparse import ArgumentParser
from collections.abc import Callable
from typing import Any

Arg = tuple[tuple[Any, ...], dict[str, Any]]


class Command:
    def __init__(self, target: Callable[..., None]) -> None:
        self.target = target
        self.name: str | None = None
        self.help: str | None = None
        self.args: list[Arg] = []
        self.completer: Callable[..., list[str]] | None = None

    def add_arg(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.args.append((args, kwargs))

    def set_completer(self, func: Callable[..., list[str]]) -> None:
        self.completer = func

    def set_info(self, name: str, help: str | None = None):
        self.name = name
        self.help = help

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return self.target(*args, **kwargs)


def _process_command(action: Callable[[Command], None]) -> Callable[..., Command]:
    def _inner(x: Command | Callable[..., None]) -> Command:
        if not isinstance(x, Command):
            x = Command(x)
        action(x)
        return x

    return _inner


def arg(*args: Any, **kwargs: Any) -> Callable[..., Command]:
    return _process_command(lambda command: command.add_arg(args, kwargs))


def completer(func: Callable[..., list[str]]) -> Callable[..., Command]:
    return _process_command(lambda command: command.set_completer(func))


class CliCommands:
    def __init__(self) -> None:
        self.commands: list[Command] = []

    def command(self, name: str, help: str | None = None) -> Callable[..., Command]:
        return _process_command(lambda command: command.set_info(name, help))

    def build_subcommands(self, p: ArgumentParser) -> None:
        subcommands = p.add_subparsers(required=True)
        for command in self.commands:
            if command.name is None:
                raise ValueError("Command has no name")
            subcommand = subcommands.add_parser(command.name, help=command.help)
            for arg in command.args:
                subcommand.add_argument(*arg[0], **arg[1])
            subcommand.set_defaults(func=command.target)
