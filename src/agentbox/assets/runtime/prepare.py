#!/usr/bin/python3
"""Mask project-declared outputs with guest-local storage, including new worktrees.

This is the sole sudo-enabled operation. Its configuration is a read-only mount;
cache ancestors are root-owned, and targets may not traverse symlinks. Guest root
is not a separate hardened boundary: the VM and explicitly shared host paths are.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path, PurePosixPath

CONFIG = Path("/etc/agentbox/project.json")
CACHE = Path("/var/cache/agentbox")


def worktrees(root: Path) -> list[Path]:
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-c",
            "safe.directory=*",
            "-C",
            str(root),
            "worktree",
            "list",
            "--porcelain",
            "-z",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    paths = [root]
    if result.returncode == 0:
        paths.extend(
            Path(p.removeprefix("worktree "))
            for p in result.stdout.split("\0")
            if p.startswith("worktree ")
        )
    return list(dict.fromkeys(p for p in paths if p.is_dir() and p.resolve().is_relative_to(root)))


def target_directory(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or ".." in parts or parts[0] == ".git":
        raise ValueError(f"Unsafe artifact directory: {relative}")
    target = root
    for part in parts:
        target /= part
        if target.is_symlink():
            raise ValueError(f"Artifact directories cannot traverse symlinks: {target}")
        target.mkdir(exist_ok=True)
    return target


def trust_worktrees(checkouts: list[Path]) -> None:
    # VirtioFS directory ownership differs from the non-root agent's UID. Trust
    # only the checkouts we actually share, not every repository in the guest.
    result = subprocess.run(
        ["/usr/bin/git", "config", "--system", "--get-all", "safe.directory"],
        text=True,
        capture_output=True,
        check=False,
    )
    trusted = set(result.stdout.splitlines())
    for checkout in checkouts:
        if str(checkout) not in trusted:
            subprocess.run(
                ["/usr/bin/git", "config", "--system", "--add", "safe.directory", str(checkout)],
                check=True,
            )


def prepare(config: dict, cache_root: Path = CACHE) -> None:
    root = Path(config["root"])
    checkouts = worktrees(root)
    trust_worktrees(checkouts)
    for checkout in checkouts:
        key = hashlib.sha256(str(checkout).encode()).hexdigest()[:20]
        for artifact in config["artifacts"]:
            target = target_directory(checkout, artifact)
            if os.path.ismount(target):
                continue
            cache = cache_root / key / artifact
            cache.mkdir(parents=True, exist_ok=True)
            os.chown(cache, 10001, 10001)
            subprocess.run(["/usr/bin/mount", "--bind", str(cache), str(target)], check=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run through the configured sudo helper")
    os.umask(0o022)
    with Path("/run/agentbox-prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        prepare(json.loads(CONFIG.read_text()))


if __name__ == "__main__":
    main()
