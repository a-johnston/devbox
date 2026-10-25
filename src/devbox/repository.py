"""Git operations between host repositories and instance clones.

Clones are agent-controlled: the host never runs Git inside one. Cloning or fetching from a
clone only runs upload-pack there, which ignores repository settings that could run commands.
"""

from __future__ import annotations

import base64
import os
import shutil
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
    # No local branch is published when all its commits are already in the host HEAD.
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
    _copy_lfs_objects(source, target)
    git(target, "reset", "--quiet", "--hard")
    branch = git_output(target, "symbolic-ref", "--quiet", "--short", "HEAD")
    return branch, git(target, "rev-parse", "HEAD").strip()


def _copy_lfs_objects(source: Path, target: Path) -> None:
    """Seed the clone's LFS store from the source's: with no remote, checkout cannot download."""
    store = git_output(source, "rev-parse", "--path-format=absolute", "--git-path", "lfs/objects")
    # Fails without git-lfs, which also means checkout has no LFS filter to satisfy.
    listing = git_output(target, "lfs", "ls-files", "--long", "HEAD")
    if store is None or not listing:
        return
    for line in listing.splitlines():
        oid = line.split(" ", 1)[0]
        if len(oid) != 64 or not oid.isalnum():
            continue
        relative = Path(oid[:2], oid[2:4], oid)
        # Objects the source never downloaded stay missing, as they would be on the host.
        if (Path(store) / relative).is_file() and not (Path(store) / relative).is_symlink():
            destination = git_directory(target) / "lfs/objects" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(store) / relative, destination)


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
    # Snapshot the primary checkout's current HEAD, not the instance's creation base.
    # Triple-dot diffs use each branch's merge base and exclude host-only changes.
    head = git(repository, "rev-parse", "--verify", "HEAD").strip()
    fetched = []
    for ref, new in refs(repository, namespace).items():
        old = before.get(ref)
        changed = git(repository, "diff", "--name-only", "-z", f"{head}...{new}", "--", *watched)
        commits = int(git(repository, "rev-list", "--count", f"{head}..{new}"))
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


def land(repository: Path, fetched: Fetched) -> str:
    """Rebase and fast-forward merge a published branch, then delete the landed local branch.

    A failed rebase deliberately stays in progress in the host checkout. Never run Git
    in the instance clone, reset a locally modified branch, or stash the user's work.
    """
    branch = fetched.branch
    if branch is None or not fetched.branch_current:
        raise DevboxError("Cannot land: the fetched local branch is unavailable or changed locally")
    source = f"refs/heads/{branch}"
    for state in (
        "rebase-merge",
        "rebase-apply",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
    ):
        path = Path(git(repository, "rev-parse", "--git-path", state).strip())
        if (path if path.is_absolute() else repository / path).exists():
            raise DevboxError(
                f"Cannot land in {repository}: a Git operation is already in progress"
            )
    target = git_output(repository, "symbolic-ref", "--quiet", "HEAD")
    if target is None or not target.startswith("refs/heads/"):
        raise DevboxError(f"Cannot land in {repository}: check out a target branch first")
    if target == source:
        raise DevboxError("Cannot land a branch onto itself; check out the target branch first")
    if git(repository, "status", "--porcelain", "--untracked-files=all").strip():
        raise DevboxError(f"Cannot land in {repository}: the host checkout must be clean")
    # Recheck after selection: fetch must not have left a stale or locally edited branch.
    tip = git(repository, "rev-parse", "--verify", fetched.ref).strip()
    if (
        git_output(repository, "symbolic-ref", "--quiet", source) is not None
        or git_output(repository, "show-ref", "--verify", "--hash", source) != tip
    ):
        raise DevboxError(f"Cannot land: {branch!r} no longer matches the fetched branch")
    # Even an unchanged branch cannot be rebased while checked out in another worktree.
    worktrees = git(repository, "worktree", "list", "--porcelain", "-z")
    if f"branch {source}" in worktrees.split("\0"):
        raise DevboxError(f"Cannot land: {branch!r} is checked out in a worktree")
    target_name = target.removeprefix("refs/heads/")
    try:
        git(repository, "rebase", "--no-autostash", "--no-update-refs", target, branch)
    except DevboxError as exc:
        raise DevboxError(
            f"{exc}\nRebase failed in {repository}; any rebase state has been left intact. "
            f"Target branch: {target_name!r}; local branch: {branch!r}. "
            "Resolve and run git rebase --continue, then check out the target branch "
            "and git merge --ff-only the rebased local branch; or git rebase --abort, "
            "check out the target branch and devbox sync it to fix it in the instance."
        ) from exc
    git(repository, "checkout", "--quiet", target_name)
    git(repository, "merge", "--ff-only", "--no-autostash", source)
    git(repository, "branch", "--delete", "--", branch)
    return target_name


def show_diff(repository: Path, ref: str, *options: str) -> None:
    """Diff from the merge base with the current host HEAD, using the user's pager."""
    argv = ["git", "-C", str(repository), "diff", *options, f"HEAD...{ref}", "--"]
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
