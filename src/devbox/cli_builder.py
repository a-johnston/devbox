"""Decorators for declaring argparse subcommands beside their handlers."""

from argparse import ArgumentParser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Completer = Callable[..., list[str]]


@dataclass
class Argument:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    completer: Completer | None = None
    exclusive_group: str | None = None


class Command:
    def __init__(self, target: Callable[..., None]) -> None:
        self.target = target
        self.name: str | None = None
        self.help: str | None = None
        self.args: list[Argument] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        return self.target(*args, **kwargs)


def _process_command(action: Callable[[Command], None]) -> Callable[[Command | Callable[..., None]], Command]:
    def _inner(command: Command | Callable[..., None]) -> Command:
        if not isinstance(command, Command):
            command = Command(command)
        action(command)
        return command

    return _inner


def arg(
    *args: Any,
    completer: Completer | None = None,
    exclusive_group: str | None = None,
    **kwargs: Any,
) -> Callable[[Command | Callable[..., None]], Command]:
    """Add an argument, in source order, with optional completion and exclusion.

    Arguments with the same exclusive_group name share a mutually exclusive
    group within their subcommand. Other keyword arguments go to argparse.
    """
    return _process_command(lambda command: command.args.insert(0, Argument(args, kwargs, completer, exclusive_group)))


class CliCommands:
    def __init__(self) -> None:
        self.commands: list[Command] = []

    def command(self, name: str, help: str | None = None) -> Callable[[Command | Callable[..., None]], Command]:
        """Register a handler; place this outside its argument decorators."""

        def register(command: Command) -> None:
            if command in self.commands:
                raise ValueError("Command is already registered")
            if any(existing.name == name for existing in self.commands):
                raise ValueError(f"Duplicate command name: {name}")
            command.name = name
            command.help = help
            self.commands.append(command)

        return _process_command(register)

    def build_subcommands(self, parser: ArgumentParser) -> None:
        subcommands = parser.add_subparsers(required=True)
        for command in self.commands:
            if command.name is None:
                raise ValueError("Command has no name")
            # Omitting help preserves argparse's behavior for commands without a summary.
            subcommand = (
                subcommands.add_parser(command.name)
                if command.help is None
                else subcommands.add_parser(command.name, help=command.help)
            )
            groups = {}
            for argument in command.args:
                destination = subcommand
                if argument.exclusive_group is not None:
                    if argument.exclusive_group not in groups:
                        groups[argument.exclusive_group] = subcommand.add_mutually_exclusive_group()
                    destination = groups[argument.exclusive_group]
                action = destination.add_argument(*argument.args, **argument.kwargs)
                if argument.completer is not None:
                    # Argcomplete reads this attribute, which argparse does not declare.
                    action.completer = argument.completer  # ty: ignore[unresolved-attribute]
            # Keep the decorated callable's identity (also used by standalone dispatch).
            subcommand.set_defaults(func=command)
