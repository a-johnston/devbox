"""Small argparse CLI; explicit arguments work without a terminal or fzf."""

# PYTHON_ARGCOMPLETE_OK

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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


def query(ask: str, default_yes: bool = False) -> bool:
    try:
        response = input(f"{ask} {'[Y/n]' if default_yes else '[y/N]'} ").lower().strip()
        return (response or ("y" if default_yes else "n")) == "y"
    except EOFError:
        return False


def require_terminal(message: str = "A terminal is required") -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DevboxError(message)


def _fzf[T](
    items: Sequence[T],
    prompt: str,
    label: Callable[[T], str],
    multiple: bool,
) -> str:
    if not items:
        raise DevboxError(f"No eligible {prompt.lower()}")
    require_terminal()
    if shutil.which("fzf") is None:
        raise DevboxError("Interactive selection requires fzf")
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
        ]
        + (["-m"] if multiple else []),
        input="\n".join(rows) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 130:
        raise KeyboardInterrupt
    if result.returncode == 1:
        if multiple:
            return ""
        raise KeyboardInterrupt
    if result.returncode:
        raise DevboxError(f"fzf failed ({result.returncode})")
    return result.stdout


def select[T](items: Sequence[T], prompt: str, label: Callable[[T], str]) -> T:
    output = _fzf(items, prompt, label, False)
    return items[int(output.split("\t", 1)[0])]


def select_many[T](items: Sequence[T], prompt: str, label: Callable[[T], str]) -> list[T]:
    output = _fzf(items, prompt, label, True)
    return [items[int(row.split("\t", 1)[0])] for row in output.splitlines()]


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def cleanup(app: App, *, select: bool) -> None:
    require_terminal("Cleanup requires a terminal for confirmation")
    instances = app.orphaned_instances()

    def label(instance: Instance) -> str:
        return f"{instance.id}  ~{format_size(instance.size)}"

    if select and instances:
        instances = select_many(instances, "Orphaned instances", label)
        if not instances:
            print("No instances selected. Nothing removed.")
            return
    images = app.unused_images(instances)
    if not instances and not images:
        print("No orphaned instances or unused Devbox images.")
        return
    if instances:
        print(f"Orphaned instances in {app.store.instances}:")
        for instance in instances:
            print(f"  {label(instance)}")
        print("This permanently deletes instance files, including unfetched repository clones.")
    if images:
        print("Unused Devbox image references:")
        for image in images:
            print(f"  {image.reference}")
        print("Images can be rebuilt; shared layers and build caches may remain.")
    size = format_size(sum(instance.size for instance in instances))
    targets = f"{len(instances)} instances (~{size}) and {len(images)} images"
    if not query(f"Delete {targets}?"):
        print("Cancelled. Nothing removed.")
        return
    removed, recovered = app.cleanup(instances)
    print(f"Removed {removed} instances; approximately {format_size(recovered)} recovered.")
    removed_images = app.cleanup_images(images)
    print(f"Removed {removed_images} image references.")


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
    return f"{container.id}  {container.harness}  {container.state}  {container.project}  {container.created}"


def session_label(session: Session) -> str:
    return f"{session.id}  {session.harness}  {session.project}"


def _run(app: App, *, harness: str | None, project: Path, no_config: bool, detach: bool) -> None:
    selected_harness = get_harness(harness)
    selected_project = Project.discover(project)
    if not detach:
        require_terminal()
    id = app.new(selected_project, selected_harness, no_config=no_config)
    if not detach:
        app.attach(next(c for c in app.engine.containers() if c.id == id))


def _build(app: App, *, harness: str | None, project: Path, no_config: bool) -> None:
    print(app.build(Project.discover(project), get_harness(harness), no_config=no_config))


def _containers(app: App, *, running: bool = False) -> list[Container]:
    app.engine.require_running()
    containers = app.engine.containers()
    return [c for c in containers if c.state == "running"] if running else containers


def _attach(app: App, *, id: str | None) -> None:
    container = match(
        _containers(app, running=True), id, "Containers", lambda c: c.id, container_label
    )
    require_terminal()
    app.attach(container)


def _shell(app: App, *, id: str | None) -> None:
    container = match(
        _containers(app, running=True), id, "Containers", lambda c: c.id, container_label
    )
    require_terminal()
    app.shell(container)


def _selected_containers(app: App, ids: list[str], *, running: bool) -> list[Container]:
    containers = _containers(app, running=running)
    if not ids:
        return select_many(containers, "Containers", container_label)
    selected = [match(containers, id, "Containers", lambda c: c.id, container_label) for id in ids]
    return list({c.id: c for c in selected}.values())


def _stop(app: App, *, ids: list[str]) -> None:
    for container in _selected_containers(app, ids, running=True):
        app.engine.stop(container.id)
        print(f"Stopped {container.id}. Conversation files retained; the live process is gone.")


def _rm(app: App, *, ids: list[str]) -> None:
    for container in _selected_containers(app, ids, running=False):
        app.engine.remove(container.id)
        print(f"Removed {container.id}. Saved conversations and repository clone retained.")


def _ls(app: App, *, json_output: bool) -> None:
    containers = _containers(app)
    if json_output:
        print(json.dumps([asdict(c) for c in containers], indent=2))
    else:
        for container in containers:
            print(container_label(container))


def _sessions(app: App, *, json_output: bool) -> None:
    sessions = app.store.sessions()
    if json_output:
        print(json.dumps([asdict(s) for s in sessions], default=str, indent=2))
    else:
        for session in sessions:
            print(session_label(session))


def _resume(
    app: App,
    *,
    session: str | None,
    project: Path | None,
    source: str | None,
    detach: bool,
    no_config: bool,
) -> None:
    selected = match(app.store.sessions(), session, "Sessions", lambda s: s.id, session_label)
    has_clone = selected.instance in app.store.clones()
    if source == "instance" and not has_clone:
        raise DevboxError(f"{selected.instance} has no repository clone; use --from host")
    from_instance = source != "host" and has_clone
    print(
        f"Cloning {selected.instance}'s repository."
        if from_instance
        else "Cloning the host checkout.",
        flush=True,
    )
    if not detach:
        require_terminal()
    id = app.new(
        Project.discover(project or Path(selected.project)),
        get_harness(selected.harness),
        no_config=no_config,
        session=selected,
        from_instance=from_instance,
    )
    if not detach:
        app.attach(next(c for c in app.engine.containers() if c.id == id))


def printable(value: str) -> str:
    """Branch and file names come from the guest: never let them drive the terminal."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in value)


def _fetch(app: App, *, id: str | None) -> None:
    def label(instance: str) -> str:
        metadata = app.store.metadata(instance)
        return f"{instance}  {metadata.get('harness')}  {metadata.get('project')}"

    instance = match(app.store.clones(), id, "Instances", lambda i: i, label)
    fetched = app.fetch(instance)
    if not fetched:
        print(f"{instance} has no branches.")
        return
    for branch in fetched:
        notes = [f"{branch.commits} new commit{'' if branch.commits == 1 else 's'}"]
        if branch.rewritten:
            notes.append("REWRITTEN since the last fetch")
        if branch.watched:
            notes.append("CHANGES " + ", ".join(printable(path) for path in branch.watched))
        print(f"{printable(branch.ref)}  {'  '.join(notes)}")
    base = app.store.metadata(instance)["base"]
    print(f"Review: git diff {base[:12]}...<ref>    Land: git merge --ff-only <ref>")


def _sync(app: App, *, id: str | None, branches: list[str]) -> None:
    container = match(
        _containers(app, running=True), id, "Containers", lambda c: c.id, container_label
    )
    for ref in app.sync(container, branches):
        print(f"Updated {ref} in {container.id}; the agent can rebase onto it.")


def _kill_all(app: App, *, yes: bool) -> None:
    containers = _containers(app)
    if not containers:
        print("No managed containers.")
        return
    if not yes:
        require_terminal()
        if not query(f"Stop/remove {len(containers)} containers? Saved history is retained."):
            return
    print(f"Removed {app.kill_all()} containers. Saved conversations retained.")


def _completion(*, shell: str) -> None:
    print(argcomplete.shellcode(["devbox"], shell=shell))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Containerized coding harnesses")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="Custom state directory")
    commands = p.add_subparsers(required=True)
    for name, func in (("new", _run), ("build", _build)):
        command = commands.add_parser(name, help=f"{name.capitalize()} project/harness environment")
        command.set_defaults(func=func)
        command.add_argument("harness", nargs="?")
        command.add_argument("--project", type=Path, default=Path("."))
        command.add_argument(
            "--no-config",
            action="store_true",
            help="Do not import host harness settings, packages or credentials",
        )
        if name == "new":
            command.add_argument("--detach", action="store_true", help="Create without attaching")
    for name, func in (("attach", _attach), ("shell", _shell)):
        command = commands.add_parser(name)
        command.set_defaults(func=func)
        command.add_argument("id", nargs="?", help="Container ID")
    for name, func in (("stop", _stop), ("rm", _rm)):
        command = commands.add_parser(name)
        command.set_defaults(func=func)
        command.add_argument("ids", nargs="*", help="Container IDs")
    for name, func in (("ls", _ls), ("sessions", _sessions)):
        command = commands.add_parser(name)
        command.set_defaults(func=func)
        command.add_argument("--json", dest="json_output", action="store_true")
    command = commands.add_parser(
        "fetch", help="Fetch an instance's branches into refs/devbox/<id>/ for review"
    )
    command.set_defaults(func=_fetch)
    command.add_argument("id", nargs="?", help="Instance ID, including removed containers")
    command = commands.add_parser(
        "sync", help="Deliver host branches to a running instance as host/<branch>"
    )
    command.set_defaults(func=_sync)
    command.add_argument("id", nargs="?", help="Container ID")
    command.add_argument(
        "branches", nargs="*", help="Host branches (default: the one the instance started from)"
    )
    command = commands.add_parser("resume", help="Fork a saved conversation into a new container")
    command.set_defaults(func=_resume)
    command.add_argument("session", nargs="?")
    command.add_argument(
        "--project", type=Path, help="Override the saved project's working directory"
    )
    command.add_argument(
        "--from",
        dest="source",
        choices=("instance", "host"),
        help="Clone the session's instance (default when it has a clone) or the host checkout",
    )
    command.add_argument("--detach", action="store_true")
    command.add_argument("--no-config", action="store_true")
    command = commands.add_parser(
        "kill-all", help="Stop/remove only managed containers; retain saved history"
    )
    command.set_defaults(func=_kill_all)
    command.add_argument("--yes", action="store_true", help="Skip confirmation")
    command = commands.add_parser(
        "cleanup", help="Delete orphaned saved instances and unused Devbox images"
    )
    command.set_defaults(func=cleanup)
    command.add_argument(
        "--select",
        action="store_true",
        help="Choose instances with fzf before confirming",
    )
    command = commands.add_parser("completion", help="Print shell completion setup")
    command.set_defaults(func=_completion)
    command.add_argument("shell", choices=("bash", "zsh", "fish"))
    return p


def get_harness(name: str | None) -> Harness:
    available = harnesses()
    if name is None:
        return select(list(available.values()), "Harnesses", lambda h: h.name)
    if name not in available:
        raise DevboxError(f"Unknown harness {name!r}; available: {', '.join(available)}")
    return available[name]


def dispatch(args: argparse.Namespace, app: App | None = None) -> None:
    state_dir = args.__dict__.pop("state_dir")
    if args.func is not _completion:
        args.app = app if app is not None else App(store=Store(state_dir))
    args.__dict__.pop("func")(**args.__dict__)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    try:
        command_parser = parser()
        argcomplete.autocomplete(command_parser)
        args = command_parser.parse_args(argv)
        dispatch(args)
    except (DevboxError, OSError) as exc:
        print(f"devbox: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
