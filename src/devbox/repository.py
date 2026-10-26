"""Git operations between host repositories and instance clones.

The host never runs Git on a clone's files directly: cloning or fetching from one only runs
upload-pack there, which ignores repository settings that could run commands. Keeping a clone
in step with the host is done by a script the guest runs as the agent (see RECEIVE).
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
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


@dataclass(frozen=True)
class Tip:
    """A ref fetch copied from a clone, compared with the host by Git."""

    ref: str
    new: str
    old: str | None  # Before this fetch.
    commits: int  # Beyond the host HEAD.
    descends: bool  # From old, so an earlier review still applies.
    watched: tuple[str, ...]


@dataclass(frozen=True)
class Landed:
    target: str
    # The landed branch's tip as fetched from the instance, and as rebased onto the target.
    fetched: str
    rebased: str


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
    # Without a source store there is nothing to copy; skip starting git-lfs at all.
    if store is None or not Path(store).is_dir():
        return
    # Fails without git-lfs, which also means checkout has no LFS filter to satisfy.
    listing = git_output(target, "lfs", "ls-files", "--long", "HEAD")
    if not listing:
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


@dataclass(frozen=True)
class Started:
    """The host checkout an instance clone was made from."""

    branch: str | None  # None when the checkout is detached.
    base: str
    # Uncommitted changes to tracked files stayed behind.
    dirty: bool


def start(checkout: Path, target: Path) -> Started:
    """Clone a host checkout's committed HEAD, detached in the clone if it is on the host."""
    if git_output(checkout, "rev-parse", "--verify", "HEAD^{commit}") is None:
        raise DevboxError(f"{checkout} must be a Git repository with a commit")
    dirty = bool(git_output(checkout, "status", "--porcelain", "--untracked-files=no"))
    branch = git_output(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    # Cloning the checkout, not the primary, transfers its HEAD even when it is detached.
    _, head = clone(checkout, target)
    if branch is None:
        # Clone guesses a branch at the same commit; the host checkout is detached.
        git(target, "checkout", "--quiet", "--detach")
    return Started(branch, head, dirty)


def identity(directory: Path) -> dict[str, str]:
    """Only the author identity crosses over: no credential helpers, hooks or rewrites."""
    output = git_output(directory, "config", "--null", "--get-regexp", r"^user\.(name|email)$") or ""
    # The last value wins, as with git config --get.
    values = dict(entry.split("\n", 1) for entry in output.split("\0") if "\n" in entry)
    env = {}
    for key, name in (("user.name", "NAME"), ("user.email", "EMAIL")):
        if value := values.get(key):
            env[f"GIT_AUTHOR_{name}"] = env[f"GIT_COMMITTER_{name}"] = value
    return env


class HostGit:
    """The Git that starting an instance runs on the host. App takes it as a parameter,
    so that tests of everything else about an instance need not spawn Git."""

    def start(self, checkout: Path, target: Path) -> Started:
        return start(checkout, target)

    def clone_instance(self, source: Path, target: Path) -> str:
        """Clone another instance's clone; return what it has checked out."""
        branch, head = clone(git_directory(source), target)
        return branch or head[:12]

    def identity(self, directory: Path) -> dict[str, str]:
        return identity(directory)


def refs(repository: Path, *patterns: str) -> dict[str, str]:
    """Refs matching for-each-ref patterns and options, with their objects."""
    output = git(repository, "for-each-ref", "--format=%(refname) %(objectname)", *patterns)
    return dict(line.split(" ", 1) for line in output.splitlines())


def checked_out(repository: Path) -> set[str]:
    """Branch refs checked out in any of the repository's worktrees."""
    worktrees = git(repository, "worktree", "list", "--porcelain", "-z").split("\0")
    return {line.removeprefix("branch ") for line in worktrees if line.startswith("branch ")}


@dataclass(frozen=True)
class LocalBranches:
    """The host's local branches, read once per fetch; updates compare-and-swap against it."""

    commits: dict[str, str]
    symbolic: set[str]
    checked_out: set[str]

    @classmethod
    def read(cls, repository: Path) -> LocalBranches:
        output = git(repository, "for-each-ref", "--format=%(refname) %(objectname) %(symref)", "refs/heads/")
        commits, symbolic = {}, set()
        for line in output.splitlines():
            ref, commit, target = line.split(" ", 2)
            if target:
                symbolic.add(ref)
            else:
                commits[ref] = commit
        return cls(commits, symbolic, checked_out(repository))


def publish(tip: Tip, namespace: str, prefix: str, local: LocalBranches) -> tuple[Fetched, str | None]:
    """Decide a fetched tip's local branch without moving checked-out or locally changed branches.

    Returns the Fetched to report if any move succeeds, and the commit the local branch must
    still be at for fetch to move it (zeros: it must not exist), or None to leave it as it is.
    """
    # No local branch is published when all its commits are already in the host HEAD.
    branch = f"{prefix}/{tip.ref.removeprefix(namespace)}" if tip.commits else None
    current, expected = _branch_update(f"refs/heads/{branch}", tip, local) if branch else (False, None)
    # Rewritten since the last fetch: an earlier review no longer applies.
    return Fetched(tip.ref, tip.commits, not tip.descends, tip.watched, branch, current), expected


def _branch_update(ref: str, tip: Tip, local: LocalBranches) -> tuple[bool, str | None]:
    have = local.commits.get(ref)
    # Never follow a symbolic branch reference to some other host branch.
    if ref in local.symbolic:
        return False, None
    if have == tip.new:
        return True, None
    if (have is not None and have != tip.old) or ref in local.checked_out:
        return False, None
    return True, have or "0" * len(tip.new)


def _compare(repository: Path, head: str, ref: str, new: str, old: str | None, watched: Sequence[str]) -> Tip:
    changed = git(repository, "diff", "--name-only", "-z", f"{head}...{new}", "--", *watched)
    commits = int(git(repository, "rev-list", "--count", f"{head}..{new}"))
    descends = old is None or old == new or git_output(repository, "merge-base", "--is-ancestor", old, new) is not None
    return Tip(ref, new, old, commits, descends, tuple(changed.split("\0")[:-1]))


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
    local = LocalBranches.read(repository)
    result = []
    for ref, new in refs(repository, namespace).items():
        fetched, expected = publish(
            _compare(repository, head, ref, new, before.get(ref), watched), namespace, branch_prefix, local
        )
        # Compare-and-swap: do not clobber a branch created or moved since it was read.
        # Ref collisions (e.g. an existing <instance> branch) also leave host refs intact.
        if (
            expected is not None
            and git_output(repository, "update-ref", "--no-deref", f"refs/heads/{fetched.branch}", new, expected)
            is None
        ):
            fetched = replace(fetched, branch_current=False)
        result.append(fetched)
    return result


IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "sequencer")


@dataclass(frozen=True)
class HostCheckout:
    """The primary checkout's state that decides whether a fetched branch can land."""

    path: Path
    busy: bool  # A Git operation such as a rebase is in progress.
    head: str | None  # The checked-out ref, or None when detached.
    dirty: bool
    branches: LocalBranches
    tip: str | None  # The fetched ref's commit.

    @classmethod
    def read(cls, repository: Path, ref: str) -> HostCheckout:
        paths = git(
            repository, "rev-parse", "--path-format=absolute", *(a for s in IN_PROGRESS for a in ("--git-path", s))
        )
        return cls(
            repository,
            any(Path(path).exists() for path in paths.splitlines()),
            git_output(repository, "symbolic-ref", "--quiet", "HEAD"),
            bool(git(repository, "status", "--porcelain", "--untracked-files=all").strip()),
            LocalBranches.read(repository),
            git_output(repository, "rev-parse", "--verify", "--quiet", ref),
        )


def landing_target(checkout: HostCheckout, fetched: Fetched) -> str:
    """The branch a fetched branch lands on; refuses anything that could lose or stash work."""
    branch = fetched.branch
    if branch is None or not fetched.branch_current:
        raise DevboxError("Cannot land: the fetched local branch is unavailable or changed locally")
    source = f"refs/heads/{branch}"
    if checkout.busy:
        raise DevboxError(f"Cannot land in {checkout.path}: a Git operation is already in progress")
    if checkout.head is None or not checkout.head.startswith("refs/heads/"):
        raise DevboxError(f"Cannot land in {checkout.path}: check out a target branch first")
    if checkout.head == source:
        raise DevboxError("Cannot land a branch onto itself; check out the target branch first")
    if checkout.dirty:
        raise DevboxError(f"Cannot land in {checkout.path}: the host checkout must be clean")
    # Recheck after selection: fetch must not have left a stale or locally edited branch.
    if source in checkout.branches.symbolic or checkout.branches.commits.get(source) != checkout.tip:
        raise DevboxError(f"Cannot land: {branch!r} no longer matches the fetched branch")
    # Even an unchanged branch cannot be rebased while checked out in another worktree.
    if source in checkout.branches.checked_out:
        raise DevboxError(f"Cannot land: {branch!r} is checked out in a worktree")
    return checkout.head.removeprefix("refs/heads/")


def land(repository: Path, fetched: Fetched) -> Landed:
    """Rebase and fast-forward merge a published branch, then delete the landed local branch.

    A failed rebase deliberately stays in progress in the host checkout. Never run Git
    in the instance clone, reset a locally modified branch, or stash the user's work.
    """
    checkout = HostCheckout.read(repository, fetched.ref)
    target = landing_target(checkout, fetched)
    branch = str(fetched.branch)
    try:
        git(repository, "rebase", "--no-autostash", "--no-update-refs", f"refs/heads/{target}", branch)
    except DevboxError as exc:
        raise DevboxError(
            f"{exc}\nRebase failed in {repository}; any rebase state has been left intact. "
            f"Target branch: {target!r}; local branch: {branch!r}. "
            "Resolve and run git rebase --continue, then check out the target branch "
            "and git merge --ff-only the rebased local branch; or git rebase --abort, "
            "check out the target branch and devbox sync it to fix it in the instance."
        ) from exc
    git(repository, "checkout", "--quiet", target)
    git(repository, "merge", "--ff-only", "--no-autostash", f"refs/heads/{branch}")
    rebased = git(repository, "rev-parse", "--verify", f"refs/heads/{branch}").strip()
    git(repository, "branch", "--delete", "--", branch)
    return Landed(target, str(checkout.tip), rebased)


def show_diff(repository: Path, ref: str, *options: str) -> None:
    """Diff from the merge base with the current host HEAD, using the user's pager."""
    argv = ["git", "-C", str(repository), "diff", *options, f"HEAD...{ref}", "--"]
    os.execvpe("git", argv, git_environment())


@dataclass(frozen=True)
class Delivery:
    """Host branches for a clone that already has base and its history."""

    # Base64 bundle of the branches with commits beyond base; empty when there are none.
    data: str
    # The other branches, by name: their commits are already in base's history.
    known: dict[str, str]


def bundle(repository: Path, branches: Sequence[str], base: str) -> Delivery:
    """Bundle host branches, omitting history the clone already has from base."""
    names = [f"refs/heads/{branch}" for branch in branches]
    tips = refs(repository, *names)
    for name in names:
        if name not in tips:
            raise DevboxError(f"No branch {name.removeprefix('refs/heads/')} in {repository}")
    # A bundle that excludes base drops refs at or behind it, so those are delivered by name.
    merged = refs(repository, f"--merged={base}", *names)
    known = {name.removeprefix("refs/heads/"): tips[name] for name in names if name in merged}
    beyond = [name for name in names if name not in merged]
    if not beyond:
        return Delivery("", known)
    result = subprocess.run(
        ["git", "-C", str(repository), "bundle", "create", "--quiet", "-", *beyond, f"^{base}"],
        capture_output=True,
        check=False,
        env=git_environment(),
    )
    if result.returncode:
        raise DevboxError(f"git bundle failed: {result.stderr.decode(errors='replace').strip()}")
    return Delivery(base64.b64encode(result.stdout).decode(), known)


def receive_specs(
    delivery: Delivery, branches: Sequence[str], rewrites: Sequence[tuple[str, str, str]] = ()
) -> list[str]:
    """RECEIVE's arguments after the checkout: deliver, then rewrite, then fast-forward."""
    specs = [f"host:{name}:{commit}" for name, commit in delivery.known.items()]
    # Rewrites first: a rebased branch must not be reported as diverged from its target.
    specs += [f"rewrite:{name}:{old}:{new}" for name, old, new in rewrites]
    return specs + [f"ff:{branch}" for branch in branches]


# Runs in the guest as the agent: unpack the bundle on stdin (if any) into host/* refs, then bring
# local branches in step where that cannot lose work. Arguments: the checkout, then
# "host:<branch>:<commit>" (set host/<branch> to a commit the clone already has; reports nothing),
# "ff:<branch>" (fast-forward a local branch to host/<branch>) and "rewrite:<branch>:<old>:<new>"
# (replace <branch> by its rebased <new> tip, only if it is still at <old>). A checked-out branch
# moves only when the tree has no uncommitted changes to tracked files and no Git operation is
# under way. One line is reported per ff or rewrite; anything not moved is left alone, with the reason.
RECEIVE = f"""
c=$1; shift
b=$(mktemp) && base64 -d > "$b" || exit 1
s=0; [ -s "$b" ] && {{ git -C "$c" fetch --quiet --no-tags "$b" "+refs/heads/*:{SYNCED}*"; s=$?; }}
rm -f "$b"; [ $s -eq 0 ] || exit $s
cd "$c" || exit 1

move() {{ # branch old new description
    ref=refs/heads/$1
    if [ "$(git symbolic-ref -q HEAD)" = "$ref" ]; then
        for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD sequencer; do
            if [ -e "$(git rev-parse --git-path $op)" ]; then
                echo "$1: left alone (a Git operation is in progress); see host/$1"; return
            fi
        done
        if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
            echo "$1: left alone (uncommitted changes); see host/$1"; return
        fi
        git reset --keep --quiet "$3" && echo "$1: $4" \
            || echo "$1: left alone (reset failed); see host/$1"
    elif git worktree list --porcelain | grep -qx "branch $ref"; then
        echo "$1: left alone (checked out in another worktree); see host/$1"
    else
        git update-ref "$ref" "$3" "$2" && echo "$1: $4" \
            || echo "$1: left alone (changed meanwhile); see host/$1"
    fi
}}

ff() {{ # branch
    new=$(git rev-parse -q --verify "{SYNCED}$1^{{commit}}") || {{ echo "$1: not delivered"; return; }}
    old=$(git rev-parse -q --verify "refs/heads/$1^{{commit}}") \
        || {{ echo "$1: delivered as host/$1 (no local branch)"; return; }}
    if [ "$old" = "$new" ]; then echo "$1: up to date"
    elif git merge-base --is-ancestor "$new" "$old"; then
        echo "$1: has commits beyond host/$1; left alone"
    elif git merge-base --is-ancestor "$old" "$new"; then move "$1" "$old" "$new" "fast-forwarded"
    else echo "$1: diverged from host/$1; left alone"
    fi
}}

rewrite() {{ # branch expected-old new
    cur=$(git rev-parse -q --verify "refs/heads/$1^{{commit}}") \
        || {{ echo "$1: no longer exists; left alone"; return; }}
    if [ "$cur" = "$3" ]; then echo "$1: matches the landed commits"
    elif [ "$cur" != "$2" ]; then echo "$1: changed since it was fetched; left alone"
    elif git cat-file -e "$3^{{commit}}" 2>/dev/null; then
        move "$1" "$cur" "$3" "moved to the rebased commits"
    else echo "$1: rebased commits not delivered; left alone"
    fi
}}

for spec; do
    case $spec in
        # Fails, leaving host/<branch> unset, if the clone no longer has the commit.
        host:*) rest=${{spec#host:}}; git update-ref "{SYNCED}${{rest%%:*}}" "${{rest#*:}}" 2>/dev/null ;;
        ff:*) ff "${{spec#ff:}}" ;;
        rewrite:*)
            rest=${{spec#rewrite:}}; name=${{rest%%:*}}; rest=${{rest#*:}}
            rewrite "$name" "${{rest%%:*}}" "${{rest#*:}}" ;;
    esac
done
"""
