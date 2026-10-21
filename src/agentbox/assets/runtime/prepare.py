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
    masked: list[Path] = []
    # Prepare ancestors first, even when Git lists a nested worktree before its parent.
    for checkout in sorted(checkouts, key=lambda path: len(path.parts)):
        if any(checkout.is_relative_to(parent) for parent in masked):
            # The entire checkout is guest-local. Do not create redundant mounts
            # or recreate host worktrees hidden by a newly mounted artifact directory.
            continue
        key = hashlib.sha256(str(checkout).encode()).hexdigest()[:20]
        for artifact in config["artifacts"]:
            target = target_directory(checkout, artifact)
            if any(target.is_relative_to(parent) for parent in masked):
                continue
            if not os.path.ismount(target):
                cache = cache_root / key / artifact
                cache.mkdir(parents=True, exist_ok=True)
                os.chown(cache, 10001, 10001)
                subprocess.run(["/usr/bin/mount", "--bind", str(cache), str(target)], check=True)
            masked.append(target)


def prepare_packages(
    config: dict,
    cache_root: Path = CACHE,
    templates: Path = Path("/opt/agentbox/prepared"),
    home: Path = Path("/home/agent"),
) -> None:
    """Give this VM writable Linux packages without copying them over VirtioFS.

    Only these disposable directories cover the private home mount. Settings,
    auth and transcripts remain host-backed; mutations cannot affect other VMs.
    """
    for relative in config.get("prepared_dirs", []):
        target = target_directory(home, relative)
        if os.path.ismount(target):
            continue
        source = templates / relative
        if not source.is_dir():
            raise ValueError(f"Prepared package template missing: {relative}; rebuild the image")
        cache = cache_root / "harness" / relative
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            subprocess.run(["/usr/bin/cp", "-a", str(source), str(cache)], check=True)
        subprocess.run(["/usr/bin/mount", "--bind", str(cache), str(target)], check=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run through the configured sudo helper")
    os.umask(0o022)
    with Path("/run/agentbox-prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(CONFIG.read_text())
        prepare_packages(config)
        prepare(config)


if __name__ == "__main__":
    main()
