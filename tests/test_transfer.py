import argparse
import io
import os
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import pytest

from devbox import cli, transfer
from devbox.app import App
from devbox.cli import dispatch, parser
from devbox.config import Harness, Project
from devbox.engine import Container, Engine
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine

# Drops the `exec --user agent [--interactive] <id>` prefix and runs the guest command here.
# COPYFILE_DISABLE keeps macOS tar from adding AppleDouble ._ entries to archives.
WRAPPER = """#!/bin/sh
export COPYFILE_DISABLE=1
shift 3
[ "$1" = --interactive ] && shift
shift
exec "$@"
"""


class Guest(FakeEngine):
    """Runs guest commands locally through the real streaming exec, under a fake root."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.executable = str(root.parent / "engine")
        Path(self.executable).write_text(WRAPPER)
        Path(self.executable).chmod(0o755)
        for name in ("inbox", "outbox"):
            (root / "devbox" / name).mkdir(parents=True)

    def local(self, args: tuple[str, ...]) -> list[str]:
        return [str(self.root) + arg if arg.startswith("/devbox") else arg for arg in args]

    def exec(self, id: str, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        super().exec(id, *args, **kwargs)
        result = subprocess.run(self.local(args), text=True, capture_output=True, check=False)
        if result.returncode and kwargs.get("check", True):
            raise DevboxError(result.stderr.strip())
        return result

    def pipe(self, id: str, *args: str, **kwargs: Any) -> None:
        Engine.pipe(self, id, *self.local(args), **kwargs)


@pytest.fixture
def guest(tmp_path: Path) -> Guest:
    return Guest(tmp_path / "guest")


@pytest.fixture
def app(guest: Guest, tmp_path: Path) -> App:
    guest.rows = [Container("devbox-aabb", "running", "project", "pi", "now")]
    store = Store(tmp_path / "state")
    _, directory = store.create(["devbox-aabb"])
    (directory / "metadata.json").write_text("{}")
    return App(guest, store)


def run(app: App, *argv: str) -> None:
    dispatch(parser().parse_args(list(argv)), app)


def tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): f"-> {os.readlink(path)}"
        if path.is_symlink()
        else ("/" if path.is_dir() else path.read_text())
        for path in sorted(root.rglob("*"))
    }


def test_put_names_each_path_after_itself_flat_in_the_inbox(
    app: App, guest: Guest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads/crash.log").write_text("crash")
    assets = tmp_path / "work/assets"
    (assets / "icons").mkdir(parents=True)
    (assets / "icons/a.svg").write_text("svg")
    (assets / "current").symlink_to("icons/a.svg")
    (tmp_path / "latest").symlink_to(home / "Downloads/crash.log")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path / "work")

    run(app, "put", "aabb", "~/Downloads/crash.log", "assets", str(tmp_path / "latest"))

    assert tree(guest.root / "devbox/inbox") == {
        "assets": "/",
        "assets/current": "-> icons/a.svg",
        "assets/icons": "/",
        "assets/icons/a.svg": "svg",
        "crash.log": "crash",
        # A symlinked argument sends its target under the link's name.
        "latest": "crash",
    }


def test_put_expands_quoted_globs_into_a_directory(
    app: App, guest: Guest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in ("logs/a.log", "logs/deep/b.log", "logs/c.txt"):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(path)
    monkeypatch.chdir(tmp_path)
    run(app, "put", "aabb", "logs/**/*.log", "--dir", "run/1")
    assert tree(guest.root / "devbox/inbox/run/1") == {
        "a.log": "logs/a.log",
        "b.log": "logs/deep/b.log",
    }
    with pytest.raises(DevboxError, match="No files match"):
        run(app, "put", "aabb", "logs/*.png")
    with pytest.raises(DevboxError, match="safe relative directory"):
        run(app, "put", "aabb", "logs", "--dir", "../escape")


def test_put_checks_every_name_before_sending(
    app: App, guest: Guest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for path in ("a/x.log", "b/x.log", "new.txt"):
        (tmp_path / path).parent.mkdir(exist_ok=True)
        (tmp_path / path).write_text(path)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DevboxError, match="would both be named x.log"):
        run(app, "put", "aabb", "*/x.log")
    assert not guest.execs

    inbox = guest.root / "devbox/inbox"
    (inbox / "a").mkdir()
    (inbox / "a/old").write_text("old")
    with pytest.raises(DevboxError, match="already exists; use --force"):
        run(app, "put", "aabb", "new.txt", "a")
    assert tree(inbox) == {"a": "/", "a/old": "old"}
    run(app, "put", "aabb", "new.txt", "a", "--force")
    assert tree(inbox) == {"a": "/", "a/x.log": "a/x.log", "new.txt": "new.txt"}


def test_put_refuses_special_files(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(DevboxError, match="not a regular file or directory"):
        transfer.uploads([tmp_path / "fifo"])


def test_put_explains_an_image_without_an_inbox(app: App, guest: Guest, tmp_path: Path) -> None:
    (guest.root / "devbox/inbox").rmdir()
    (tmp_path / "a").write_text("a")
    with pytest.raises(DevboxError, match="predates the inbox; run devbox build"):
        run(app, "put", "aabb", str(tmp_path / "a"))


def test_instances_learn_about_the_inbox_and_outbox(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"))
    id = app.new(project, harness, no_config=True)
    text = (app.store.directory(id) / "home/.devbox/environment.md").read_text()
    assert "`/devbox/inbox/`" in text and f"`devbox get {id} <name>`" in text


def test_put_needs_a_running_container(app: App, tmp_path: Path) -> None:
    with pytest.raises(DevboxError, match="stopped"):
        app.put(Container("devbox-aabb", "stopped", "project", "pi", "now"), {})


def test_get_copies_outbox_and_absolute_paths_flat(
    app: App, guest: Guest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox = guest.root / "devbox/outbox"
    (outbox / "shots").mkdir()
    (outbox / "shots/1.png").write_text("one")
    (outbox / "shots/2.png").write_text("two")
    (outbox / "report.md").write_text("report")
    (guest.root / "devbox/elsewhere.txt").write_text("elsewhere")
    (outbox / "latest.png").symlink_to("shots/2.png")
    monkeypatch.chdir(tmp_path)

    run(app, "get", "aabb", "report.md", "latest.png", "/devbox/elsewhere.txt", "--to", "out")
    assert tree(tmp_path / "out") == {
        "elsewhere.txt": "elsewhere",
        "latest.png": "two",
        "report.md": "report",
    }
    run(app, "get", "aabb", "shots/*.png")
    assert (tmp_path / "1.png").read_text() == "one"
    assert (tmp_path / "2.png").read_text() == "two"
    with pytest.raises(DevboxError, match="No match in the guest"):
        run(app, "get", "aabb", "missing*")


def test_get_replaces_existing_names_only_with_force(app: App, guest: Guest, tmp_path: Path) -> None:
    (guest.root / "devbox/outbox/docs").mkdir()
    (guest.root / "devbox/outbox/docs/new.md").write_text("new")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/old.md").write_text("old")
    with pytest.raises(DevboxError, match="already exists; use --force"):
        run(app, "get", "aabb", "docs", "--to", str(tmp_path))
    run(app, "get", "aabb", "docs", "--to", str(tmp_path), "--force")
    assert tree(tmp_path / "docs") == {"new.md": "new"}
    assert not list(tmp_path.glob(".devbox-get-*"))


def archive(add: Callable[[tarfile.TarFile], None]) -> io.BytesIO:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as created:
        add(created)
    data.seek(0)
    return data


def member(created: tarfile.TarFile, name: str, **fields: Any) -> None:
    info = tarfile.TarInfo(name)
    for key, value in fields.items():
        setattr(info, key, value)
    if info.isfile():
        info.size = 1
    created.addfile(info, io.BytesIO(b"x") if info.isfile() else None)


@pytest.mark.parametrize(
    ("build", "error"),
    [
        (lambda t: member(t, "./other"), "unrequested file"),
        (
            lambda t: member(t, "./wanted", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
            "Refusing a received file",
        ),
        (
            lambda t: member(t, "./wanted", type=tarfile.SYMTYPE, linkname="../../outside"),
            "Refusing a received file",
        ),
    ],
)
def test_received_archives_extract_only_requested_names(
    tmp_path: Path, build: Callable[[tarfile.TarFile], None], error: str
) -> None:
    (tmp_path / "keep").write_text("keep")
    with pytest.raises(DevboxError, match=error):
        transfer.extract(archive(build), tmp_path, {"wanted": "wanted"})
    assert tree(tmp_path) == {"keep": "keep"}


def test_pipe_reports_the_guest_error(guest: Guest) -> None:
    def send(stream: IO[bytes]) -> None:
        stream.write(b"x" * 1_000_000)

    with pytest.raises(DevboxError, match="refused"):
        guest.pipe("devbox-aabb", "sh", "-c", "echo refused >&2; exit 3", send=send)
    with pytest.raises(DevboxError, match="broken"):
        guest.pipe(
            "devbox-aabb",
            "sh",
            "-c",
            "echo partial; echo broken >&2; exit 2",
            receive=lambda stream: transfer.extract(stream, guest.root, {"x": "x"}),
        )


def test_get_completes_guest_paths(app: App, guest: Guest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outbox = guest.root / "devbox/outbox"
    (outbox / "shots").mkdir()
    (outbox / "shots/1.png").write_text("")
    (outbox / ".hidden").write_text("")
    (outbox / "report.md").write_text("")
    monkeypatch.setattr(cli, "App", lambda store: App(guest, store))
    args = argparse.Namespace(state_dir=app.store.root.parent, id="aa")
    assert sorted(cli.complete_guest_paths("", args)) == [".hidden", "report.md", "shots/"]
    assert cli.complete_guest_paths("shots/", args) == ["shots/1.png"]
    absolute = cli.complete_guest_paths("/devbox/", args)
    assert sorted(absolute) == ["/devbox/inbox/", "/devbox/outbox/"]
    assert cli.complete_guest_paths("", argparse.Namespace(state_dir=app.store.root.parent, id="zz")) == []
