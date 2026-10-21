"""Small argparse CLI; explicit arguments work without a terminal or fzf."""

# PYTHON_ARGCOMPLETE_OK

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path

import argcomplete

from devbox import __version__
from devbox.app import App
from devbox.config import Harness, Project, harnesses
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.state import Instance, Session, Store


def require_terminal(
    message: str = "A terminal is required; supply an ID/harness, or use new --detach",
) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DevboxError(message)


def select[T](items: Sequence[T], prompt: str, label: Callable[[T], str]) -> T:
    if not items:
        raise DevboxError(f"No eligible {prompt.lower()}")
    require_terminal()
    if shutil.which("fzf") is None:
        raise DevboxError("Interactive selection requires fzf, or supply an explicit argument")
    rows = [f"{i}\t{label(item)}" for i, item in enumerate(items)]
    result = subprocess.run(
        [
            "fzf",
            "--delimiter=\t",
            "--with-nth=2..",
            "--prompt",
            f"{prompt}> ",
            "--height=60%",
            "--reverse",
        ],
        input="\n".join(rows) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode in (1, 130):
        raise KeyboardInterrupt
    if result.returncode:
        raise DevboxError("fzf failed")
    return items[int(result.stdout.split("\t", 1)[0])]


def select_many[T](items: Sequence[T], prompt: str, label: Callable[[T], str]) -> list[T]:
    require_terminal()
    if shutil.which("fzf") is None:
        raise DevboxError("Interactive selection requires fzf")
    rows = [f"{i}\t{label(item)}" for i, item in enumerate(items)]
    result = subprocess.run(
        [
            "fzf",
            "-m",
            "--delimiter=\t",
            "--with-nth=2..",
            "--prompt",
            f"{prompt}> ",
            "--header=Tab to select; Enter to review; Esc to cancel",
            "--height=60%",
            "--reverse",
        ],
        input="\n".join(rows) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 130:
        raise KeyboardInterrupt
    if result.returncode == 1:
        return []
    if result.returncode:
        raise DevboxError("fzf failed")
    return [items[int(row.split("\t", 1)[0])] for row in result.stdout.splitlines()]


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def cleanup(app: App, *, select: bool) -> None:
    instances = app.orphaned_instances()
    if not instances:
        print("No orphaned instances.")
        return

    def label(instance: Instance) -> str:
        return f"{instance.id}  ~{format_size(instance.size)}"

    if select:
        instances = select_many(instances, "Orphaned instances", label)
    if not instances:
        print("No instances selected.")
        return
    print(f"Orphaned instances in {app.store.instances}:")
    for instance in instances:
        print(f"  {label(instance)}")
    print("This permanently deletes their saved conversations, copied credentials and home files.")
    require_terminal("Cleanup requires a terminal for confirmation")
    size = format_size(sum(instance.size for instance in instances))
    try:
        answer = input(f"Delete {len(instances)} instances (~{size})? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() != "y":
        print("Cancelled. No instances removed.")
        return
    removed, recovered = app.cleanup(instances)
    print(f"Removed {removed} instances; approximately {format_size(recovered)} recovered.")
    if removed < len(instances):
        print("Instances that now have containers or no longer exist were skipped.")


def match[T](
    items: Sequence[T],
    value: str | None,
    prompt: str,
    key: Callable[[T], str],
    label: Callable[[T], str],
) -> T:
    if value is None:
        return select(items, prompt, label)
    exact = [item for item in items if key(item) == value]
    candidates = exact or [
        item for item in items if key(item).removeprefix("devbox-").startswith(value)
    ]
    if len(candidates) != 1:
        raise DevboxError(f"{value!r} matches {len(candidates)} {prompt.lower()}; use a full ID")
    return candidates[0]


def container_label(container: Container) -> str:
    return f"{container.id}  {container.harness:8}  {container.state:8}  {container.project}  {container.created}"


def _display_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in text
    )


def session_labels(sessions: Sequence[Session]) -> dict[str, str]:
    """Shorten unambiguous project paths and align columns across the whole catalog."""
    projects: dict[str, set[Path]] = {}
    for session in sessions:
        path = Path(session.project)
        projects.setdefault(path.name, set()).add(path)
    rows = []
    for session in sessions:
        name = Path(session.project).name
        project = name if name and len(projects[name]) == 1 else session.project
        rows.append((session.harness, project, session.title or "(untitled)", session.id))
    widths = [max((_display_width(row[i]) for row in rows), default=0) for i in range(3)]
    return {
        row[3]: "  ".join(
            text + " " * (width - _display_width(text))
            for text, width in zip(row[:3], widths, strict=True)
        )
        + "  "
        + row[3]
        for row in rows
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Coding harnesses in disposable containers")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="Override the durable state directory")
    commands = p.add_subparsers(dest="action", required=True)
    for name in ("new", "build"):
        command = commands.add_parser(
            name, help=f"{name.capitalize()} a project/harness environment"
        )
        command.add_argument("harness", nargs="?")
        command.add_argument("--project", type=Path, default=Path("."))
        command.add_argument(
            "--no-config",
            action="store_true",
            help="Do not import host harness settings, packages or credentials",
        )
        if name == "new":
            command.add_argument("--detach", action="store_true", help="Create without attaching")
    for name in ("attach", "shell", "stop", "rm"):
        command = commands.add_parser(name)
        command.add_argument("id", nargs="?", help="Full or unambiguous short container ID")
    for name in ("ls", "sessions"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
    command = commands.add_parser("resume", help="Fork a saved conversation into a new container")
    command.add_argument("session", nargs="?")
    command.add_argument(
        "--project", type=Path, help="Override the saved project's working directory"
    )
    command.add_argument("--detach", action="store_true")
    command.add_argument("--no-config", action="store_true")
    command = commands.add_parser(
        "kill-all", help="Stop/remove only managed containers; retain saved history"
    )
    command.add_argument("--yes", action="store_true", help="Skip confirmation")
    command = commands.add_parser(
        "cleanup", help="Delete saved instances without containers, including their conversations"
    )
    command.add_argument(
        "--select",
        action="store_true",
        help="Choose instances with fzf multi-select before confirming",
    )
    command = commands.add_parser("completion", help="Print shell completion setup")
    command.add_argument("shell", choices=("bash", "zsh", "fish"))
    return p


def get_harness(name: str | None) -> Harness:
    available = harnesses()
    if name is None:
        return select(list(available.values()), "Harnesses", lambda h: h.name)
    if name not in available:
        raise DevboxError(f"Unknown harness {name!r}; available: {', '.join(available)}")
    return available[name]


def dispatch(args: argparse.Namespace, app: App) -> None:
    if args.action == "cleanup":
        cleanup(app, select=args.select)
        return
    if args.action in ("new", "build"):
        harness = get_harness(args.harness)
        project = Project.discover(args.project)
        if args.action == "build":
            print(app.build(project, harness, no_config=args.no_config))
            return
        if not args.detach:
            require_terminal()
        id = app.new(project, harness, no_config=args.no_config)
        if not args.detach:
            app.attach(next(c for c in app.engine.containers() if c.id == id))
        return
    if args.action in ("sessions", "resume"):
        sessions = app.store.sessions()
        labels = session_labels(sessions)
        if args.action == "sessions":
            if args.json:
                print(json.dumps([asdict(s) for s in sessions], default=str, indent=2))
            else:
                for session in sessions:
                    print(labels[session.id])
            return
        session = match(sessions, args.session, "Sessions", lambda s: s.id, lambda s: labels[s.id])
        if not args.detach:
            require_terminal()
        project = Project.discover(args.project or Path(session.project))
        id = app.new(
            project,
            get_harness(session.harness),
            no_config=args.no_config,
            session=session,
        )
        if not args.detach:
            app.attach(next(c for c in app.engine.containers() if c.id == id))
        return
    app.engine.require_running()
    containers = app.engine.containers()
    if args.action == "ls":
        if args.json:
            print(json.dumps([asdict(c) for c in containers], indent=2))
        else:
            for container in containers:
                print(container_label(container))
    elif args.action == "kill-all":
        if not containers:
            print("No managed containers.")
            return
        if not args.yes:
            require_terminal()
            if (
                input(
                    f"Stop/remove {len(containers)} managed containers? Saved history is retained. [y/N] "
                ).lower()
                != "y"
            ):
                return
        print(f"Removed {app.kill_all()} containers. Saved conversations retained.")
    else:
        eligible = (
            containers if args.action == "rm" else [c for c in containers if c.state == "running"]
        )
        container = match(eligible, args.id, "Containers", lambda c: c.id, container_label)
        if args.action in ("attach", "shell"):
            require_terminal()
            getattr(app, args.action)(container)
        elif args.action == "stop":
            app.engine.stop(container.id)
            print(f"Stopped {container.id}. Conversation files retained; the live process is gone.")
        elif args.action == "rm":
            app.engine.remove(container.id)
            print(f"Removed {container.id}. Saved conversations retained.")


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        command_parser = parser()
        argcomplete.autocomplete(command_parser)
        args = command_parser.parse_args(argv)
        if args.action == "completion":
            print(argcomplete.shellcode(["devbox"], shell=args.shell))
        else:
            dispatch(args, App(store=Store(args.state_dir)))
    except (DevboxError, OSError) as exc:
        print(f"devbox: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
