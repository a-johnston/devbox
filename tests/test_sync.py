import base64
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App
from devbox.config import Harness, Project
from devbox.engine import Container
from devbox.errors import DevboxError
from devbox.repository import Delivery, Git, Started
from devbox.state import Session, Store
from devbox.sync import APPLY, INSPECT, MISSING, GuestBranch, GuestClone, Move, inspect_arguments, plan, report
from tests.fakes import BASE, FakeEngine, FakeRepository
from tests.repos import commit, git


def running(id: str) -> Container:
    return Container(id, "running", "project", "pi", "now")


# plan: what the host decides for each branch from the clone's reported state.

LOCAL, HOST, NEW = "1" * 40, "2" * 40, "3" * 40


def state(
    head: str | None = "refs/heads/work",
    busy: bool = False,
    dirty: bool = False,
    checked_out: set[str] | None = None,
    branches: dict[str, GuestBranch] | None = None,
    commits: set[str] | None = None,
) -> GuestClone:
    return GuestClone(head, busy, dirty, checked_out or set(), branches or {}, commits or set())


@pytest.mark.parametrize(
    ("branch", "line"),
    [
        (MISSING, "x: not delivered"),
        (GuestBranch(None, HOST, False, False), "x: delivered as host/x (no local branch)"),
        (GuestBranch(HOST, HOST, True, True), "x: up to date"),
        (GuestBranch(LOCAL, HOST, True, False), "x: has commits beyond host/x; left alone"),
        (GuestBranch(LOCAL, HOST, False, False), "x: diverged from host/x; left alone"),
    ],
)
def test_plan_leaves_branches_that_cannot_fast_forward(branch: GuestBranch, line: str) -> None:
    assert plan(state(branches={"x": branch}), ["x"]) == [line]


def test_plan_fast_forwards_a_branch_behind_the_host() -> None:
    clone = state(branches={"x": GuestBranch(LOCAL, HOST, False, True)})
    assert plan(clone, ["x"]) == [Move("x", LOCAL, HOST, "fast-forwarded", reset=False)]


@pytest.mark.parametrize(
    ("local", "commits", "outcome"),
    [
        (None, {NEW}, "x: no longer exists; left alone"),
        (NEW, {NEW}, "x: matches the landed commits"),
        (HOST, {NEW}, "x: changed since it was fetched; left alone"),
        (LOCAL, set(), "x: rebased commits not delivered; left alone"),
        (LOCAL, {NEW}, Move("x", LOCAL, NEW, "moved to the rebased commits", reset=False)),
    ],
)
def test_plan_rewrites_a_branch_only_while_it_is_unchanged(
    local: str | None, commits: set[str], outcome: str | Move
) -> None:
    clone = state(branches={"x": GuestBranch(local, None, False, False)}, commits=commits)
    assert plan(clone, [], [("x", LOCAL, NEW)]) == [outcome]


@pytest.mark.parametrize(
    ("clone", "outcome"),
    [
        (state(head="refs/heads/x"), Move("x", LOCAL, HOST, "fast-forwarded", reset=True)),
        (state(head="refs/heads/x", dirty=True), "x: left alone (uncommitted changes); see host/x"),
        (state(head="refs/heads/x", busy=True), "x: left alone (a Git operation is in progress); see host/x"),
        (state(checked_out={"refs/heads/x"}), "x: left alone (checked out in another worktree); see host/x"),
        # Uncommitted changes elsewhere do not stop a branch that is not checked out.
        (state(dirty=True, busy=True, head=None), Move("x", LOCAL, HOST, "fast-forwarded", reset=False)),
    ],
)
def test_plan_moves_only_what_loses_nothing(clone: GuestClone, outcome: str | Move) -> None:
    clone = replace(clone, branches={"x": GuestBranch(LOCAL, HOST, False, True)})
    assert plan(clone, ["x"]) == [outcome]


def test_plan_decides_rewrites_before_fast_forwards() -> None:
    # A rebased branch must not be reported as diverged from its target.
    clone = state(branches={"x": GuestBranch(NEW, NEW, True, True), "y": GuestBranch(NEW, None, False, False)})
    assert plan(clone, ["x"], [("y", LOCAL, NEW)]) == ["y: matches the landed commits", "x: up to date"]


def test_inspect_arguments_deliver_and_report_every_branch_once() -> None:
    delivery = Delivery("", {"release": HOST})
    assert inspect_arguments(delivery, ["main", "feature"], [("feature", LOCAL, NEW)]) == [
        f"host:release:{HOST}",
        "branch:feature",
        "branch:main",
        f"commit:{NEW}",
    ]


def test_report_describes_each_applied_move() -> None:
    steps: list[str | Move] = [
        "a: up to date",
        Move("b", LOCAL, HOST, "fast-forwarded", reset=False),
        Move("c", LOCAL, HOST, "fast-forwarded", reset=False),
        Move("d", LOCAL, NEW, "moved to the rebased commits", reset=True),
        Move("e", LOCAL, NEW, "moved to the rebased commits", reset=True),
    ]
    assert report(steps, ["ok", "changed", "failed"]) == [
        "a: up to date",
        "b: fast-forwarded",
        "c: left alone (changed meanwhile); see host/c",
        "d: left alone (reset failed); see host/d",
        "e: left alone (reset failed); see host/e",  # APPLY stopped before reporting it.
    ]


def test_guest_clone_parses_inspect_output() -> None:
    output = (
        "head refs/heads/work\nbusy 1\ndirty 0\nworktree refs/heads/work\nworktree refs/heads/linked\n"
        f"branch main {LOCAL} {HOST} 0 1\nbranch gone - - 0 0\ncommit {NEW}\n"
    )
    assert GuestClone.parse(output) == GuestClone(
        "refs/heads/work",
        busy=True,
        dirty=False,
        checked_out={"refs/heads/work", "refs/heads/linked"},
        branches={"main": GuestBranch(LOCAL, HOST, False, True), "gone": MISSING},
        commits={NEW},
    )
    assert GuestClone.parse("head -\nbusy 0\ndirty 0\n").head is None


# The guest scripts against a real clone: INSPECT reports, APPLY moves only while facts hold.


def advance(repository: Path) -> str:
    git(repository, "commit", "--allow-empty", "-qm", "upstream")
    return git(repository, "rev-parse", "main")


def branches(repository: Path, **targets: str) -> None:
    """Create branches at the given commits in one Git process."""
    lines = "".join(f"create refs/heads/{name} {commit}\n" for name, commit in targets.items())
    subprocess.run(["git", "-C", str(repository), "update-ref", "--stdin"], input=lines, text=True, check=True)


def guest(script: str, clone: Path, *arguments: str, data: str = "") -> str:
    result = subprocess.run(
        ["sh", "-c", script, "sh", str(clone), *arguments], input=data, text=True, check=True, capture_output=True
    )
    return result.stdout


def test_inspect_delivers_host_branches_and_reports_the_clone(repository: Path, clone: Path, tmp_path: Path) -> None:
    base = git(clone, "rev-parse", "HEAD")
    git(clone, "switch", "-qc", "work")
    ahead = git(clone, "commit-tree", "-m", "guest work", "-p", base, f"{base}^{{tree}}")
    branches(clone, ahead=ahead, same=base, linked=base)
    git(clone, "worktree", "add", "--quiet", str(tmp_path / "linked"), "linked")
    (clone / ".devbox.toml").write_text("edited")
    (clone / ".git/rebase-merge").mkdir()
    tip = advance(repository)
    branches(repository, same=base, ahead=base)
    delivery = Git().bundle(repository, ["main", "same", "ahead"], base)
    output = guest(
        INSPECT,
        clone,
        *inspect_arguments(delivery, ["main", "same", "ahead", "nowhere"], [("work", base, ahead)]),
        f"host:lost:{'f' * 40}",  # A known commit the clone no longer has.
        "branch:lost",
        f"commit:{'f' * 40}",
        data=delivery.data,
    )
    assert GuestClone.parse(output) == GuestClone(
        "refs/heads/work",
        busy=True,
        dirty=True,
        checked_out={"refs/heads/work", "refs/heads/linked"},
        branches={
            "work": GuestBranch(base, None, False, False),
            "main": GuestBranch(base, tip, False, True),
            "same": GuestBranch(base, base, True, True),
            "ahead": GuestBranch(ahead, base, True, False),
            "nowhere": MISSING,
            "lost": MISSING,
        },
        commits={ahead},
    )


def test_apply_moves_branches_whose_facts_still_hold(clone: Path, tmp_path: Path) -> None:
    base = git(clone, "rev-parse", "HEAD")
    new = git(clone, "commit-tree", "-m", "rebased", "-p", base, f"{base}^{{tree}}")
    old = commit(clone, "feature", {"a": "1"})
    branches(clone, idle=base, moved=new, linked=base)
    git(clone, "worktree", "add", "--quiet", str(tmp_path / "linked"), "linked")
    (clone / "untracked").write_text("kept")
    moves = [f"reset:feature:{old}:{new}", f"update:idle:{base}:{new}", f"update:moved:{base}:{old}"]
    assert guest(APPLY, clone, *moves, f"update:linked:{base}:{new}").split() == ["ok", "ok", "changed", "changed"]
    assert git(clone, "rev-parse", "HEAD", "feature", "idle", "moved", "linked").split() == [new, new, new, new, base]
    assert git(clone, "symbolic-ref", "--short", "HEAD") == "feature"
    assert git(clone, "status", "--porcelain") == "?? untracked"


@pytest.mark.parametrize("change", ["edited", "rebasing", "switched", "moved"])
def test_apply_leaves_a_checked_out_branch_that_changed(clone: Path, change: str) -> None:
    base = git(clone, "rev-parse", "HEAD")
    new = git(clone, "commit-tree", "-m", "rebased", "-p", base, f"{base}^{{tree}}")
    if change == "edited":
        (clone / ".devbox.toml").write_text("edited")
    elif change == "rebasing":
        (clone / ".git/rebase-merge").mkdir()
    elif change == "switched":
        git(clone, "switch", "-qc", "other")
    else:
        git(clone, "commit", "--allow-empty", "-qm", "more")
    assert guest(APPLY, clone, f"reset:main:{base}:{new}").split() == ["changed"]
    assert git(clone, "rev-parse", "main") != new


def test_bundle_delivers_branches_at_or_behind_base_by_name(repository: Path, tmp_path: Path) -> None:
    root = git(repository, "rev-parse", "main")
    base = advance(repository)
    git(repository, "branch", "at-base")
    git(repository, "branch", "behind", root)
    advance(repository)
    delivery = Git().bundle(repository, ["main", "at-base", "behind"], base)
    assert delivery.known == {"at-base": base, "behind": root}
    data = tmp_path / "data.bundle"
    data.write_bytes(base64.b64decode(delivery.data))
    heads = git(repository, "bundle", "list-heads", str(data)).splitlines()
    assert [line.split()[1] for line in heads] == ["refs/heads/main"]
    # Nothing beyond base: no bundle at all, rather than the branches' whole history.
    assert Git().bundle(repository, ["at-base"], base) == Delivery("", {"at-base": base})


def test_bundle_refuses_missing_branches(repository: Path) -> None:
    with pytest.raises(DevboxError, match="No branch missing"):
        Git().bundle(repository, ["missing"], git(repository, "rev-parse", "main"))


# Instances: what they are told, where forks start, and which branches App.sync delivers.


def test_instance_learns_its_identity_and_constraints(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeRepository())
    id = app.new(project, harness, no_config=True)
    assert engine.runs[0].env["DEVBOX_INSTANCE"] == id
    text = (app.store.directory(id) / "home/.devbox/environment.md").read_text()
    assert f"{project.workspace} is a private clone of the user's repository, started from main" in text
    assert f"`devbox review {id}`" in text and f"`devbox sync {id} [branch ...]`" in text


def test_clone_instance_continues_from_its_checked_out_branch(clone: Path, tmp_path: Path) -> None:
    work = commit(clone, "work", {"a": "1"})
    target = tmp_path / "fork"
    assert Git().clone_instance(clone, target) == "work"
    assert git(target, "rev-parse", "HEAD") == work
    assert git(target, "symbolic-ref", "--short", "HEAD") == "work"


def test_fork_from_instance_keeps_the_original_host_starting_point(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeRepository(Started("release", BASE, dirty=False)))
    first = app.new(project, harness, no_config=True)
    source = app.store.directory(first) / "home/source.jsonl"
    source.write_text('{"type":"session","id":"s"}\n')
    session = Session(f"{first}/s", first, "pi", str(project.cwd), source)
    app.repository = FakeRepository(Started("main", "f" * 40, dirty=False))

    resumed = app.new(project, harness, no_config=True, session=session, from_instance=True)
    metadata = app.store.metadata(resumed)
    assert (metadata["branch"], metadata["base"]) == ("release", BASE)
    text = (app.store.directory(resumed) / "home/.devbox/environment.md").read_text()
    assert "started from copied" in text  # What the fake's clone_instance checked out.

    fresh = app.new(project, harness, no_config=True, session=session)
    assert (app.store.metadata(fresh)["branch"], app.store.metadata(fresh)["base"]) == ("main", "f" * 40)


def test_sync_refusals(project: Project, harness: Harness, tmp_path: Path) -> None:
    app = App(FakeEngine(), Store(tmp_path / "state"), FakeRepository())
    id = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="stopped"):
        app.sync(replace(running(id), state="stopped"), [])
    app.repository = FakeRepository(Started(None, BASE, dirty=False))
    detached = app.new(project, harness, no_config=True)
    with pytest.raises(DevboxError, match="name branches"):
        app.sync(running(detached), [])


def test_sync_inspects_plans_and_applies_in_the_guest(project: Project, harness: Harness, tmp_path: Path) -> None:
    engine = FakeEngine()
    repository = FakeRepository()
    app = App(engine, Store(tmp_path / "state"), repository)
    id = app.new(project, harness, no_config=True)
    repository.delivery = Delivery("BUNDLE", {"release": BASE})
    engine.execs.clear()
    engine.replies = [
        f"head refs/heads/feature\nbusy 0\ndirty 0\nbranch feature {LOCAL} - 0 0\n"
        f"branch main {LOCAL} {HOST} 0 1\ncommit {NEW}\n",
        "ok\nchanged\n",
    ]
    rewrites = [("feature", LOCAL, NEW)]
    assert app.sync(running(id), [], rewrites) == [
        "feature: moved to the rebased commits",
        "main: left alone (changed meanwhile); see host/main",
    ]
    # Defaults to the branch the instance started from.
    assert repository.calls == [("bundle", project.root, ["main"], BASE)]
    inspect, apply = engine.execs
    workspace = str(project.workspace)
    arguments = inspect_arguments(repository.delivery, ["main"], rewrites)
    assert (inspect.args, inspect.input) == (("sh", "-c", INSPECT, "sh", workspace, *arguments), "BUNDLE")
    moves = (f"reset:feature:{LOCAL}:{NEW}", f"update:main:{LOCAL}:{HOST}")
    assert apply.args == ("sh", "-c", APPLY, "sh", workspace, *moves)

    # Nothing to move needs no second round. Instances created before the workspace move
    # mount their clone at the host checkout.
    metadata = app.store.metadata(id)
    del metadata["workspace"]
    (app.store.directory(id) / "metadata.json").write_text(json.dumps(metadata))
    engine.execs.clear()
    engine.replies = [f"head -\nbusy 0\ndirty 0\nbranch main {HOST} {HOST} 1 1\n"]
    assert app.sync(running(id), ["main"]) == ["main: up to date"]
    (inspect,) = engine.execs
    assert inspect.args[4] == metadata["checkout"]
