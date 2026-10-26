"""Git helpers for tests that exercise real repositories."""

import subprocess
from pathlib import Path


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), "-c", "user.name=T", "-c", "user.email=t@e.invalid", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(clone: Path, branch: str, files: dict[str, str]) -> str:
    git(clone, "checkout", "--quiet", "-B", branch, "main")
    for name, text in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
    git(clone, "add", "--", *files)
    git(clone, "commit", "--quiet", "-m", f"change {branch}")
    return git(clone, "rev-parse", "HEAD")
