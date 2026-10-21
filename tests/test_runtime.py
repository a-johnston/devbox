import shutil
import subprocess
from pathlib import Path

import pytest

from agentbox.assets.runtime import command, prepare


def test_preparation_does_not_copy_host_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    output = root / ".godot"
    output.mkdir()
    sentinel = output / "host-only"
    sentinel.write_text("host")
    calls = []
    monkeypatch.setattr(prepare, "worktrees", lambda root: [root])
    monkeypatch.setattr(prepare, "trust_worktrees", lambda roots: None)
    monkeypatch.setattr(prepare.os, "chown", lambda *a: None)
    monkeypatch.setattr(prepare.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    prepare.prepare({"root": str(root), "artifacts": [".godot"]}, tmp_path / "cache")
    assert sentinel.read_text() == "host"
    assert len(calls) == 1
    assert calls[0][:2] == ["/usr/bin/mount", "--bind"]
    assert list(Path(calls[0][2]).iterdir()) == []
    assert calls[0][3] == str(output)


def test_prepared_packages_are_private_guest_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    templates, home, cache = (tmp_path / name for name in ("templates", "home", "cache"))
    source = templates / ".pi/agent/npm"
    source.mkdir(parents=True)
    (source / "package.json").write_text("prepared")
    (source / "link").symlink_to("package.json")
    target = home / ".pi/agent/npm"
    target.mkdir(parents=True)
    (target / "host-only").write_text("host cache must not be copied")
    auth = home / ".pi/agent/auth.json"
    auth.write_text("private")
    calls = []

    def run(args: list[str], **kwargs: object) -> None:
        calls.append(args)
        if args[0] == "/usr/bin/cp":
            shutil.copytree(args[2], args[3], symlinks=True)

    monkeypatch.setattr(prepare.subprocess, "run", run)
    prepare.prepare_packages({"prepared_dirs": [".pi/agent/npm"]}, cache, templates, home)
    clone = cache / "harness/.pi/agent/npm"
    assert (clone / "link").is_symlink()
    assert (clone / "package.json").read_text() == "prepared"
    assert not (clone / "host-only").exists()
    (clone / "package.json").write_text("changed by this VM")
    assert (source / "package.json").read_text() == "prepared"
    assert auth.read_text() == "private"
    assert calls[-1] == ["/usr/bin/mount", "--bind", str(clone), str(target)]
    monkeypatch.setattr(prepare.os.path, "ismount", lambda p: True)
    prepare.prepare_packages({"prepared_dirs": [".pi/agent/npm"]}, cache, templates, home)
    assert len(calls) == 2


def test_symlink_artifact_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "tests").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        prepare.target_directory(root, "tests/obj")


def test_worktree_discovery_only_includes_mounted_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = tmp_path / "repo", tmp_path / "outside"
    inside = root / ".worktrees/feature"
    inside.mkdir(parents=True)
    outside.mkdir()
    output = f"worktree {root}\0HEAD abc\0\0worktree {inside}\0HEAD abc\0\0worktree {outside}\0HEAD abc\0\0"
    monkeypatch.setattr(
        prepare.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, output, "")
    )
    assert prepare.worktrees(root) == [root, inside]


def test_git_worktree_add_prepares_before_and_after(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(command.sys, "argv", ["/opt/agentbox/bin/git", "worktree", "add", "new"])
    monkeypatch.setattr(command.Path, "read_text", lambda self: '{"git":"/usr/bin/git"}')

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(command.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        command.main()
    assert exc.value.code == 0
    assert calls[0] == calls[2] == ["/usr/bin/sudo", "-n", "/opt/agentbox/prepare.py"]
    assert calls[1] == ["/usr/bin/git", "worktree", "add", "new"]


def test_failed_git_add_still_prepares_for_partial_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(command.sys, "argv", ["git", "worktree", "add", "new"])
    monkeypatch.setattr(command.Path, "read_text", lambda self: '{"git":"/usr/bin/git"}')

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 7 if args[0] == "/usr/bin/git" else 0, "", "")

    monkeypatch.setattr(command.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        command.main()
    assert exc.value.code == 7
    assert len(calls) == 3
    assert calls[2] == ["/usr/bin/sudo", "-n", "/opt/agentbox/prepare.py"]
