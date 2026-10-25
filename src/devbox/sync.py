"""Bringing an instance clone's branches in step with the host's without losing work.

The guest only reports its clone's state (INSPECT) and applies the moves the host chose
(APPLY), each only while the facts the host relied on still hold. The host decides every
branch from that report (see plan). The host never runs Git in the clone; only the
guest's Git does, as the agent inside the container.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from devbox.repository import IN_PROGRESS, Delivery

# Host branches delivered by sync, beside the origin/* snapshot taken at clone time.
SYNCED = "refs/remotes/host/"

# Prints 1 if a Git operation such as a rebase is in progress in the current repository, else 0.
_BUSY = f"""busy() {{
    for op in {" ".join(IN_PROGRESS)}; do
        [ -e "$(git rev-parse --git-path $op)" ] && {{ echo 1; return; }}
    done
    echo 0
}}"""

# Runs in the guest as the agent. Unpacks the bundle on stdin (if any) into host/* refs, then
# reports the clone's state, one fact per line. Arguments: the checkout, then
# "host:<branch>:<commit>" (set host/<branch> to a commit the clone already has; if it no longer
# does, host/<branch> stays unset), "branch:<name>" (report a branch) and "commit:<sha>"
# (report the commit if the clone has it).
INSPECT = f"""
c=$1; shift
b=$(mktemp) && base64 -d > "$b" || exit 1
s=0; [ -s "$b" ] && {{ git -C "$c" fetch --quiet --no-tags "$b" "+refs/heads/*:{SYNCED}*"; s=$?; }}
rm -f "$b"; [ $s -eq 0 ] || exit $s
cd "$c" || exit 1
{_BUSY}
tip() {{ git rev-parse -q --verify "$1^{{commit}}" || echo -; }}
for spec; do
    case $spec in
        host:*) rest=${{spec#host:}}; git update-ref "{SYNCED}${{rest%%:*}}" "${{rest#*:}}" 2>/dev/null ;;
    esac
done
echo "head $(git symbolic-ref -q HEAD || echo -)"
echo "busy $(busy)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then echo "dirty 1"; else echo "dirty 0"; fi
git worktree list --porcelain | sed -n 's/^branch /worktree /p'
for spec; do
    case $spec in
        branch:*)
            n=${{spec#branch:}}; l=$(tip "refs/heads/$n"); h=$(tip "{SYNCED}$n"); hl=0; lh=0
            if [ "$l" != - ] && [ "$h" != - ]; then
                git merge-base --is-ancestor "$h" "$l" && hl=1
                git merge-base --is-ancestor "$l" "$h" && lh=1
            fi
            echo "branch $n $l $h $hl $lh" ;;
        commit:*) x=${{spec#commit:}}; if git cat-file -e "$x^{{commit}}" 2>/dev/null; then echo "commit $x"; fi ;;
    esac
done
"""

# Runs in the guest as the agent: applies the host's moves, "reset:<branch>:<old>:<new>" for the
# checked-out branch and "update:<branch>:<old>:<new>" for any other. Each prints "ok", "changed"
# when a fact the host relied on no longer holds, or "failed".
APPLY = f"""
cd "$1" || exit 1; shift
{_BUSY}
for spec; do
    kind=${{spec%%:*}}; rest=${{spec#*:}}; name=${{rest%%:*}}; rest=${{rest#*:}}; old=${{rest%%:*}}; new=${{rest#*:}}
    ref=refs/heads/$name
    if [ "$kind" = reset ]; then
        if [ "$(git symbolic-ref -q HEAD)" != "$ref" ] || [ "$(git rev-parse -q --verify HEAD)" != "$old" ] \\
            || [ "$(busy)" = 1 ] || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
            echo changed
        elif git reset --keep --quiet "$new"; then echo ok
        else echo failed
        fi
    elif [ "$(git symbolic-ref -q HEAD)" = "$ref" ] || git worktree list --porcelain | grep -qx "branch $ref"; then
        echo changed
    elif git update-ref "$ref" "$new" "$old" 2>/dev/null; then echo ok
    else echo changed
    fi
done
"""


@dataclass(frozen=True)
class GuestBranch:
    local: str | None
    host: str | None
    host_in_local: bool  # The host tip is in the local branch's history.
    local_in_host: bool


MISSING = GuestBranch(None, None, False, False)


@dataclass(frozen=True)
class GuestClone:
    """An instance clone's state, as INSPECT reports it."""

    head: str | None  # The checked-out ref, or None when detached.
    busy: bool  # A Git operation such as a rebase is in progress.
    dirty: bool  # Uncommitted changes to tracked files.
    checked_out: set[str]  # Branch refs checked out in any worktree.
    branches: dict[str, GuestBranch]
    commits: set[str]  # Reported commits the clone has.

    @classmethod
    def parse(cls, output: str) -> GuestClone:
        facts: dict[str, str] = {}
        checked_out, branches, commits = set(), {}, set()
        for line in output.splitlines():
            kind, _, value = line.partition(" ")
            if kind == "worktree":
                checked_out.add(value)
            elif kind == "branch":
                name, local, host, host_in_local, local_in_host = value.split(" ")
                branches[name] = GuestBranch(
                    None if local == "-" else local,
                    None if host == "-" else host,
                    host_in_local == "1",
                    local_in_host == "1",
                )
            elif kind == "commit":
                commits.add(value)
            else:
                facts[kind] = value
        head = facts.get("head", "-")
        return cls(
            None if head == "-" else head,
            facts.get("busy") == "1",
            facts.get("dirty") == "1",
            checked_out,
            branches,
            commits,
        )


@dataclass(frozen=True)
class Move:
    branch: str
    old: str
    new: str
    done: str  # What the report says once it moved.
    reset: bool  # The checked-out branch moves with its worktree.

    @property
    def argument(self) -> str:
        return f"{'reset' if self.reset else 'update'}:{self.branch}:{self.old}:{self.new}"


def inspect_arguments(
    delivery: Delivery, branches: Sequence[str], rewrites: Sequence[tuple[str, str, str]]
) -> list[str]:
    """INSPECT's arguments after the checkout, for syncing branches and rewrites."""
    names = dict.fromkeys([*(name for name, _, _ in rewrites), *branches])
    return [
        *(f"host:{name}:{commit}" for name, commit in delivery.known.items()),
        *(f"branch:{name}" for name in names),
        *(f"commit:{new}" for _, _, new in rewrites),
    ]


def plan(
    clone: GuestClone,
    branches: Sequence[str],
    rewrites: Sequence[tuple[str, str, str]] = (),
    *,
    force: bool = False,
) -> list[str | Move]:
    """Decide each rewrite, then each branch to fast-forward: a report line, or a move to apply.

    A rewrite (branch, old, new) replaces a branch by its rebased tip only while it is still at
    old; rewrites come first, so a rebased branch is not reported as diverged from its target.
    Force also resets branches that are diverged from or ahead of the host's, still never
    over uncommitted changes or a Git operation in progress.
    """
    steps = [_rewrite(clone, *rewrite) for rewrite in rewrites]
    return steps + [_fast_forward(clone, branch, force) for branch in branches]


def _fast_forward(clone: GuestClone, name: str, force: bool) -> str | Move:
    branch = clone.branches.get(name, MISSING)
    if branch.host is None:
        return f"{name}: not delivered"
    if branch.local is None:
        return f"{name}: delivered as host/{name} (no local branch)"
    if branch.local == branch.host:
        return f"{name}: up to date"
    if branch.local_in_host:
        return _move(clone, name, branch.local, branch.host, "fast-forwarded")
    if force:
        return _move(clone, name, branch.local, branch.host, f"reset to host/{name}")
    if branch.host_in_local:
        return f"{name}: has commits beyond host/{name}; left alone (devbox sync --force replaces them)"
    return f"{name}: diverged from host/{name}; left alone (devbox sync --force replaces it)"


def _rewrite(clone: GuestClone, name: str, old: str, new: str) -> str | Move:
    local = clone.branches.get(name, MISSING).local
    if local is None:
        return f"{name}: no longer exists; left alone"
    if local == new:
        return f"{name}: matches the landed commits"
    if local != old:
        return f"{name}: changed since it was fetched; left alone"
    if new not in clone.commits:
        return f"{name}: rebased commits not delivered; left alone"
    return _move(clone, name, old, new, "moved to the rebased commits")


def _move(clone: GuestClone, name: str, old: str, new: str, done: str) -> str | Move:
    """Move a branch only where that loses nothing: a checked-out branch only when its tree
    has no uncommitted changes to tracked files and no Git operation is under way."""
    ref = f"refs/heads/{name}"
    if clone.head == ref:
        if clone.busy:
            return f"{name}: left alone (a Git operation is in progress); see host/{name}"
        if clone.dirty:
            return f"{name}: left alone (uncommitted changes); see host/{name}"
        return Move(name, old, new, done, reset=True)
    if ref in clone.checked_out:
        return f"{name}: left alone (checked out in another worktree); see host/{name}"
    return Move(name, old, new, done, reset=False)


def report(steps: Sequence[str | Move], results: Sequence[str]) -> list[str]:
    """One line per step, given APPLY's result for each move in order; a moved branch's line
    names its old tip, which stays in the clone's reflog."""
    outcomes = iter(results)
    lines = []
    for step in steps:
        if isinstance(step, str):
            lines.append(step)
            continue
        outcome = next(outcomes, "failed")
        if outcome == "ok":
            lines.append(f"{step.branch}: {step.done} (was {step.old[:12]})")
        else:
            reason = "reset failed" if outcome == "failed" and step.reset else "changed meanwhile"
            lines.append(f"{step.branch}: left alone ({reason}); see host/{step.branch}")
    return lines
