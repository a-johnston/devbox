"""Git operations between host repositories and instance clones.

Clones are agent-controlled: the host never runs Git inside one. Cloning or fetching from a
clone only runs upload-pack there, which ignores repository settings that could run commands.
"""

from __future__ import annotations

import base64
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from devbox.config import git, git_environment, git_output
from devbox.errors import DevboxError

# Host branches delivered by sync, beside the origin/* snapshot taken at clone time.
SYNCED = "refs/remotes/host/"


@dataclass(frozen=True)
class Fetched:
    ref: str
    commits: int
    rewritten: bool
    # Changed image inputs and manifest-declared paths, relative to the repository root.
    watched: tuple[str, ...]
    # No local branch is published when there are no commits beyond the starting point.
    branch: str | None
    branch_current: bool


def git_directory(clone: Path) -> Path:
    """An instance clone's own .git, never one redirected elsewhere on the host."""
    directory = clone / ".git"
    if directory.is_symlink() or not directory.is_dir():
        raise DevboxError(f"{clone} is not a repository clone")
    return directory


def clone(source: Path, target: Path) -> tuple[str | None, str]:
    """Clone source's committed HEAD (branch or detached) into a remote-less target."""
    # Transfer reachable objects only: no hardlinks a guest running as the host user could
    # rewrite, and no unreachable objects (dropped stashes, other instances' fetched work).
    git(target.parent, "clone", "--quiet", "--no-local", "--no-checkout", str(source), str(target))
    # Origin would name the guest's own mount path; its refs stay as a snapshot of the source.
    git(target, "config", "--remove-section", "remote.origin")
    git(target, "reset", "--quiet", "--hard")
    branch = git_output(target, "symbolic-ref", "--quiet", "--short", "HEAD")
    return branch, git(target, "rev-parse", "HEAD").strip()


def refs(repository: Path, namespace: str) -> dict[str, str]:
    output = git(repository, "for-each-ref", "--format=%(refname) %(objectname)", namespace)
    return dict(line.split(" ", 1) for line in output.splitlines())


def local_branch(repository: Path, name: str, new: str, old: str | None) -> bool:
    """Publish a fetched tip without moving checked-out or locally changed branches."""
    ref = f"refs/heads/{name}"
    # Never follow a symbolic branch reference to some other host branch.
    if git_output(repository, "symbolic-ref", "--quiet", ref) is not None:
        return False
    current = git_output(repository, "show-ref", "--verify", "--hash", ref)
    if current == new:
        return True
    if current is not None and current != old:
        return False
    worktrees = git(repository, "worktree", "list", "--porcelain", "-z")
    if f"branch {ref}" in worktrees.split("\0"):
        return False
    # Compare-and-swap: do not clobber a branch created or moved since we checked it.
    # Ref collisions (e.g. an existing <instance> branch) also leave host refs intact.
    return (
        git_output(repository, "update-ref", "--no-deref", ref, new, current or "0" * len(new))
        is not None
    )


def fetch(
    repository: Path,
    clone: Path,
    namespace: str,
    base: str,
    watched: Sequence[str],
    *,
    branch_prefix: str,
) -> list[Fetched]:
    """Copy clone refs under namespace; publish new work under a concise local prefix."""
    before = refs(repository, namespace)
    git(
        repository,
        "-c",
        "transfer.fsckObjects=true",
        "fetch",
        "--quiet",
        "--no-tags",
        "--prune",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        str(git_directory(clone)),
        f"+refs/heads/*:{namespace}*",
    )
    fetched = []
    for ref, new in refs(repository, namespace).items():
        old = before.get(ref)
        changed = git(repository, "diff", "--name-only", "-z", f"{base}...{new}", "--", *watched)
        commits = int(git(repository, "rev-list", "--count", f"{base}..{new}"))
        branch = f"{branch_prefix}/{ref.removeprefix(namespace)}" if commits else None
        fetched.append(
            Fetched(
                ref,
                commits,
                # Rewritten since the last fetch: an earlier review no longer applies.
                old is not None
                and old != new
                and git_output(repository, "merge-base", "--is-ancestor", old, new) is None,
                tuple(changed.split("\0")[:-1]),
                branch,
                branch is not None and local_branch(repository, branch, new, old),
            )
        )
    return fetched


def show_diff(repository: Path, base: str, ref: str, *options: str) -> None:
    """Replace this process with git diff in the host repository, using the user's pager."""
    argv = ["git", "-C", str(repository), "diff", *options, f"{base}...{ref}", "--"]
    os.execvpe("git", argv, git_environment())


def bundle(repository: Path, branches: Sequence[str], base: str) -> str:
    """Base64 bundle of host branches; omits history the clone already has from base."""
    names = [f"refs/heads/{branch}" for branch in branches]
    for name in names:
        if git_output(repository, "show-ref", "--verify", "--quiet", name) is None:
            raise DevboxError(f"No branch {name.removeprefix('refs/heads/')} in {repository}")
    # Git refuses an empty bundle, so exclude base only when something lies beyond it.
    new = int(git(repository, "rev-list", "--count", *names, f"^{base}"))
    result = subprocess.run(
        ["git", "-C", str(repository), "bundle", "create", "--quiet", "-", *names]
        + ([f"^{base}"] if new else []),
        capture_output=True,
        check=False,
        env=git_environment(),
    )
    if result.returncode:
        raise DevboxError(f"git bundle failed: {result.stderr.decode(errors='replace').strip()}")
    return base64.b64encode(result.stdout).decode()


# Runs in the guest as the agent: unpack the bundle on stdin and update host/* refs.
RECEIVE = (
    'b=$(mktemp) && base64 -d > "$b" && git -C "$1" fetch --quiet --no-tags "$b"'
    f' "+refs/heads/*:{SYNCED}*"; s=$?; rm -f "$b"; exit $s'
)
