import subprocess
from pathlib import Path

import pytest
from fakes import FakeEngine

from devbox.app import App
from devbox.cli import dispatch, format_size, parser
from devbox.errors import DevboxError
from devbox.state import Store, disk_usage


class InventoryEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.available = True
        self.queries = 0

    def require_running(self) -> None:
        if not self.available:
            raise DevboxError("Container service unavailable")

    def container_ids(self) -> set[str]:
        self.queries += 1
        return self.ids

    def remove(self, id: str) -> None:
        raise AssertionError("State cleanup must not remove containers or volumes")


@pytest.fixture
def app(tmp_path: Path) -> App:
    return App(InventoryEngine(), Store(tmp_path / "state"))


def saved_instance(store: Store) -> tuple[str, Path]:
    id, directory = store.create()
    (directory / "metadata.json").write_text("{}")
    (directory / "home/session.jsonl").write_text('{"type":"session","id":"saved"}\n')
    (directory / "home/auth.json").write_bytes(b"x" * 8192)
    return id, directory


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda *args: None)


def test_cleanup_removes_only_orphans(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, InventoryEngine)
    active, active_dir = saved_instance(app.store)
    stopped, stopped_dir = saved_instance(app.store)
    orphan, orphan_dir = saved_instance(app.store)
    # Labels are deliberately absent: any matching container protects the instance.
    engine.ids = {active, stopped}
    candidates = app.orphaned_instances()
    assert [instance.id for instance in candidates] == [orphan]
    assert candidates[0].size == disk_usage(orphan_dir)
    assert app.cleanup(candidates) == (1, candidates[0].size)
    assert engine.queries == 2
    assert active_dir.exists() and stopped_dir.exists()
    assert not orphan_dir.exists()


def test_cleanup_includes_incomplete_launches_without_metadata(app: App) -> None:
    id, directory = app.store.create()
    (directory / "home/settings.json").write_text("{}")
    candidates = app.orphaned_instances()
    assert [instance.id for instance in candidates] == [id]
    assert app.cleanup(candidates)[0] == 1
    assert not directory.exists()


def test_cleanup_rechecks_inventory_and_only_removes_confirmed_instances(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, InventoryEngine)
    protected, protected_dir = saved_instance(app.store)
    missing, missing_dir = saved_instance(app.store)
    removed, removed_dir = saved_instance(app.store)
    confirmed = app.orphaned_instances()
    app.store.remove_instance(missing)
    new, new_dir = saved_instance(app.store)
    engine.ids = {protected}
    expected = next(instance.size for instance in confirmed if instance.id == removed)
    assert app.cleanup(confirmed + confirmed) == (1, expected)
    assert protected_dir.exists() and new_dir.exists()
    assert not missing_dir.exists() and not removed_dir.exists()
    assert {instance.id for instance in app.orphaned_instances()} == {new}


def test_cleanup_inventory_failure_preserves_state(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, directory = saved_instance(app.store)

    def fail() -> set[str]:
        raise DevboxError("Cannot determine containers; refusing cleanup")

    monkeypatch.setattr(app.engine, "container_ids", fail)
    with pytest.raises(DevboxError, match="refusing cleanup"):
        app.orphaned_instances()
    assert directory.exists()


def test_cleanup_service_failure_after_confirmation_preserves_state(app: App) -> None:
    _, directory = saved_instance(app.store)
    candidates = app.orphaned_instances()
    engine = app.engine
    assert isinstance(engine, InventoryEngine)
    engine.available = False
    with pytest.raises(DevboxError, match="Container service"):
        app.cleanup(candidates)
    assert directory.exists()


def test_cleanup_does_not_follow_symlinks_or_count_targets(app: App, tmp_path: Path) -> None:
    id, directory = saved_instance(app.store)
    outside = tmp_path / "outside"
    outside.mkdir()
    data = outside / "important"
    data.write_bytes(b"x" * 1024 * 1024)
    (directory / "home/external-dir").symlink_to(outside, target_is_directory=True)
    (directory / "home/external-file").symlink_to(data)
    (directory / "home/prepared").symlink_to("/opt/devbox/prepared/not-on-host")
    assert disk_usage(directory) < disk_usage(outside)
    # The metadata is not trusted for paths, and need not be valid to reclaim the directory.
    (directory / "metadata.json").write_text("not valid JSON")
    assert app.cleanup(app.orphaned_instances())[0] == 1
    assert not (app.store.instances / id).exists()
    assert data.read_bytes() == b"x" * 1024 * 1024


def test_disk_usage_counts_allocated_blocks_and_deduplicates_hardlinks(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    directory.mkdir()
    file = directory / "file"
    file.write_bytes(b"x" * 8192)
    (directory / "link").hardlink_to(file)
    sparse = directory / "sparse"
    with sparse.open("wb") as stream:
        stream.truncate(1024 * 1024 * 1024)
    expected = sum(path.lstat().st_blocks * 512 for path in (directory, file, sparse))
    assert disk_usage(directory) == expected
    assert disk_usage(directory) < sparse.stat().st_size


def test_cleanup_ignores_unrecognized_and_symlinked_directories(app: App, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "metadata.json").write_text("{}")
    app.store.instances.mkdir(parents=True)
    (app.store.instances / "linked").symlink_to(outside, target_is_directory=True)
    unrelated = app.store.instances / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep").write_text("keep")
    (app.store.instances / "file").write_text("keep")
    metadata_link = app.store.instances / "metadata-link"
    metadata_link.mkdir()
    (metadata_link / "metadata.json").symlink_to(outside / "metadata.json")
    assert app.orphaned_instances() == []
    for id in ("linked", "unrelated", "../outside", str(outside), "metadata-link"):
        with pytest.raises(DevboxError, match="Not a removable instance"):
            app.store.remove_instance(id)
    assert (outside / "metadata.json").exists()
    assert (unrelated / "keep").exists()


def test_cleanup_refuses_symlinked_instance_root(app: App, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    app.store.root.mkdir(parents=True)
    app.store.instances.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DevboxError, match="symlinked instances"):
        app.orphaned_instances()
    with pytest.raises(DevboxError, match="symlinked instances"):
        app.store.remove_instance("anything")
    assert outside.exists()


@pytest.mark.parametrize("answer", ["", "n", "yes"])
def test_cleanup_cancellation_preserves_sessions(
    app: App,
    terminal: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
) -> None:
    _, directory = saved_instance(app.store)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    dispatch(parser().parse_args(["cleanup"]), app)
    text = capsys.readouterr().out
    assert "permanently deletes" in text
    assert "saved conversations, copied credentials" in text
    assert "Cancelled. Nothing removed." in text
    assert directory.exists()


def test_cleanup_confirmation_and_summary(
    app: App, terminal: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, directory = saved_instance(app.store)
    expected = disk_usage(directory)
    prompts = []

    def confirm(prompt: str) -> str:
        assert directory.exists()
        prompts.append(prompt)
        return " Y "

    monkeypatch.setattr("builtins.input", confirm)
    dispatch(parser().parse_args(["cleanup"]), app)
    assert prompts == [f"Delete 1 instances (~{format_size(expected)})? [y/N] "]
    assert (
        f"Removed 1 instances; approximately {format_size(expected)} recovered."
        in capsys.readouterr().out
    )
    assert not directory.exists()


def test_cleanup_eof_preserves_state(
    app: App, terminal: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, directory = saved_instance(app.store)

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    dispatch(parser().parse_args(["cleanup"]), app)
    assert directory.exists()


def test_cleanup_requires_interactive_confirmation(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, directory = saved_instance(app.store)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("must not prompt without a terminal")
    )
    with pytest.raises(DevboxError, match="terminal for confirmation"):
        dispatch(parser().parse_args(["cleanup"]), app)
    assert directory.exists()


def test_empty_cleanup_does_not_prompt_or_create_state(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("nothing to confirm"))
    dispatch(parser().parse_args(["cleanup"]), app)
    assert capsys.readouterr().out == "No orphaned instances or unused Devbox images.\n"
    assert not app.store.root.exists()


def test_cleanup_multi_select_reviews_subset_before_confirming(
    app: App, terminal: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(3):
        saved_instance(app.store)
    candidates = app.orphaned_instances()
    selected = [candidates[0], candidates[2]]
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: "/bin/fzf")

    def pick(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[0:2] == ["fzf", "-m"]
        assert all(instance.id in str(kwargs["input"]) for instance in candidates)
        return subprocess.CompletedProcess(args, 0, "0\tfirst\n2\tthird\n", "")

    def confirm(prompt: str) -> str:
        text = capsys.readouterr().out
        assert all(instance.id in text for instance in selected)
        assert candidates[1].id not in text
        assert "Delete 2 instances" in prompt
        return "y"

    monkeypatch.setattr("devbox.cli.subprocess.run", pick)
    monkeypatch.setattr("builtins.input", confirm)
    dispatch(parser().parse_args(["cleanup", "--select"]), app)
    assert (app.store.instances / candidates[1].id).exists()
    assert all(not (app.store.instances / instance.id).exists() for instance in selected)
    assert "Removed 2 instances" in capsys.readouterr().out


@pytest.mark.parametrize("code", [0, 1, 130, 2])
def test_cleanup_picker_cancel_and_errors_never_delete(
    app: App, terminal: None, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    _, directory = saved_instance(app.store)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: "/bin/fzf")
    monkeypatch.setattr(
        "devbox.cli.subprocess.run",
        lambda *a, **kw: subprocess.CompletedProcess(a, code, "", ""),
    )
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("nothing selected"))
    if code == 130:
        with pytest.raises(KeyboardInterrupt):
            dispatch(parser().parse_args(["cleanup", "--select"]), app)
    elif code == 2:
        with pytest.raises(DevboxError, match="fzf failed"):
            dispatch(parser().parse_args(["cleanup", "--select"]), app)
    else:
        dispatch(parser().parse_args(["cleanup", "--select"]), app)
    assert directory.exists()


def test_cleanup_picker_requires_fzf(
    app: App, terminal: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, directory = saved_instance(app.store)
    monkeypatch.setattr("devbox.cli.shutil.which", lambda _: None)
    with pytest.raises(DevboxError, match="requires fzf"):
        dispatch(parser().parse_args(["cleanup", "--select"]), app)
    assert directory.exists()


@pytest.mark.parametrize(
    "size, expected", [(0, "0.0 B"), (1024, "1.0 KiB"), (1536, "1.5 KiB"), (1024**3, "1.0 GiB")]
)
def test_format_size(size: int, expected: str) -> None:
    assert format_size(size) == expected
