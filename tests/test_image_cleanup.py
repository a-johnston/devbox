import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App, containerfile_base
from devbox.cli import dispatch, format_size, parser
from devbox.config import Harness, Project
from devbox.engine import IMAGE_KIND, IMAGE_SCHEMA, IMAGE_STORE, MANAGED, Image, image_labels
from devbox.engine.apple import AppleEngine
from devbox.engine.engine import IMAGE_BASE, image_identity
from devbox.engine.podman import PodmanEngine
from devbox.errors import DevboxError
from devbox.state import Store, disk_usage
from tests.fakes import FakeEngine
from tests.test_cleanup import InventoryEngine, saved_instance
from tests.test_utils import patch_input, patch_require_terminal


class ImageEngine(InventoryEngine):
    def __init__(self) -> None:
        super().__init__()
        self.inventory: list[Image] = []
        self.used: set[str] = set()
        self.deleted: list[str] = []

    def images(self) -> list[Image]:
        return self.inventory

    def container_images(self) -> set[str]:
        return self.used

    def remove_image(self, reference: str) -> None:
        self.deleted.append(reference)
        self.inventory = [image for image in self.inventory if image.reference != reference]


@pytest.fixture
def app(tmp_path: Path) -> App:
    return App(ImageEngine(), Store(tmp_path / "state"))


def owned(app: App, reference: str = "unused:latest", id: str = "sha256:unused") -> Image:
    return Image(reference, id, {**app.image_owner, IMAGE_KIND: "prepared"}, size=4096)


def retain(app: App, image: str) -> str:
    id, directory = saved_instance(app.store)
    (directory / "metadata.json").write_text(json.dumps({"image": image}))
    return id


def test_new_builds_label_every_stage_and_scope_to_state_directory(
    project: Project, harness: Harness, tmp_path: Path
) -> None:
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"))
    app.build(project, harness)
    for build, kind in zip(engine.builds, ("project", "runtime", "prepared"), strict=True):
        labels = build.labels
        assert all(labels[key] == value for key, value in app.image_owner.items())
        assert labels[IMAGE_KIND] == kind
        assert labels["dev.devbox.project"] == str(project.root)
        assert labels["dev.devbox.harness"] == (harness.name if kind == "prepared" else "")
    other = App(engine, Store(tmp_path / "other-state"))
    assert app.image_owner[IMAGE_STORE] != other.image_owner[IMAGE_STORE]
    assert app._prepared_image(project, harness, {}, {}) != other._prepared_image(
        project, harness, {}, {}
    )


def test_cleanup_only_owns_labeled_images_in_this_store(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    foreign_store = replace(
        image, reference="other-store", labels={**image.labels, IMAGE_STORE: "other"}
    )
    legacy = replace(image, reference="devbox-old:latest", labels={})
    future = replace(image, reference="future", labels={**image.labels, IMAGE_SCHEMA: "2"})
    unknown = replace(image, reference="unknown", labels={**image.labels, IMAGE_KIND: "unknown"})
    engine.inventory = [image, foreign_store, legacy, future, unknown]
    assert app.unused_images() == [image]
    # The other references keep this image's bytes alive, even though we do not own them.
    assert app.cleanup_images([image, image, foreign_store, legacy, future, unknown]) == (1, {})
    assert engine.deleted == [image.reference]


def test_saved_instances_and_all_container_images_protect_aliases(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    saved = owned(app, "docker.io/library/saved:latest", "sha256:saved")
    alias = replace(saved, reference="different:tag")
    running = owned(app, "running:latest", "sha256:running")
    stopped = owned(app, "stopped:latest", "sha256:stopped")
    unused = owned(app)
    engine.inventory = [alias, saved, running, stopped, unused]
    retain(app, "saved:latest")
    # Containers may reference a digest or a qualified local tag, irrespective of ownership.
    engine.used = {"sha256:running", "localhost/stopped:latest"}
    assert app.unused_images() == [unused]
    assert app.cleanup_images([alias, saved, running, stopped, unused]) == (
        1,
        {image_identity(unused.id): unused.size},
    )
    assert engine.deleted == [unused.reference]


def test_removing_one_instance_does_not_unprotect_a_shared_image(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    first = retain(app, image.reference)
    second = retain(app, image.reference)
    instances = app.orphaned_instances()
    assert app.unused_images([item for item in instances if item.id == first]) == []
    assert app.unused_images(instances) == [image]
    # A matching container protects it even when its saved home was selected for removal.
    engine.used = {image.id}
    assert app.unused_images(instances) == []
    assert second != first


@pytest.mark.parametrize("change", ["saved", "container", "retagged", "missing", "ownership"])
def test_cleanup_rechecks_after_confirmation(app: App, change: str) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    candidates = app.unused_images()
    if change == "saved":
        retain(app, image.reference)
    elif change == "container":
        engine.used.add(image.id)
    elif change == "retagged":
        engine.inventory = [replace(image, id="sha256:replacement")]
    elif change == "ownership":
        engine.inventory = [replace(image, labels={})]
    else:
        engine.inventory = []
    assert app.cleanup_images(candidates) == (0, {})
    assert engine.deleted == []


def test_cleanup_never_adds_new_image_candidates(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    candidates = app.unused_images()
    newer = owned(app, "newer:latest", "sha256:newer")
    engine.inventory.append(newer)
    assert app.cleanup_images(candidates) == (1, {image_identity(image.id): image.size})
    assert engine.inventory == [newer]


@pytest.mark.parametrize("payload", ["invalid", "{}", "null", "[]", '{"image": null}'])
def test_unreadable_retained_metadata_blocks_image_deletion(app: App, payload: str) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    _, directory = saved_instance(app.store)
    (directory / "metadata.json").write_text(payload)
    with pytest.raises(DevboxError):
        app.cleanup_images([image])
    assert engine.deleted == []
    assert directory.exists()


def test_incomplete_retained_launch_blocks_image_cleanup(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    engine.inventory = [owned(app)]
    app.store.create()
    with pytest.raises(DevboxError, match="No saved state"):
        app.unused_images()
    assert engine.deleted == []


def test_combined_cleanup_removes_images_after_selected_homes(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    id = retain(app, image.reference)
    instance_size = disk_usage(app.store.directory(id))
    patch_require_terminal(monkeypatch)
    patch_input(monkeypatch)
    dispatch(parser().parse_args(["cleanup"]), app)
    assert engine.deleted == [image.reference]
    assert list(app.store.instances.iterdir()) == []
    text = capsys.readouterr().out
    assert f"Removed 1 instances; approximately {format_size(instance_size)} recovered." in text
    assert "Removed 1 image references; approximately 4.0 KiB recovered." in text


def test_image_cleanup_lists_sizes_before_confirmation_and_cancellation_deletes_nothing(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    sizes = [
        (4096, "~4.0 KiB"),
        (2 * 1024**2, "~2.0 MiB"),
        (0, "~0.0 B"),
        (None, "size unavailable"),
    ]
    engine.inventory = [
        replace(owned(app, f"image{n}:latest", f"sha256:image{n}"), size=size)
        for n, (size, _) in enumerate(sizes)
    ]
    patch_require_terminal(monkeypatch)

    def confirm(prompt: str) -> str:
        text = capsys.readouterr().out
        for image, (_, estimate) in zip(engine.inventory, sizes, strict=True):
            assert f"  {image.reference}  {estimate}\n" in text
        assert not engine.deleted
        return "n"

    monkeypatch.setattr("builtins.input", confirm)
    dispatch(parser().parse_args(["cleanup"]), app)
    assert engine.deleted == []


def apple_image(labels: dict[str, str] | None = None) -> dict:
    return {
        "configuration": {
            "name": "docker.io/library/ours:latest",
            "descriptor": {"digest": "sha256:index"},
        },
        "variants": [
            {
                "digest": "sha256:manifest",
                "size": 2048,
                "platform": {"os": "linux"},
                "config": {"config": {"Labels": labels or {}}},
            }
        ],
    }


def reply(
    monkeypatch: pytest.MonkeyPatch, engine: AppleEngine | PodmanEngine, payload: object
) -> list:
    calls = []

    def call(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(engine, "call", call)
    return calls


def test_apple_image_inventory_uses_oci_labels_and_variant_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AppleEngine()
    labels = {MANAGED: "1", IMAGE_KIND: "prepared"}
    row = apple_image(labels)
    # Descriptor annotations are not the image's OCI config ownership labels.
    row["configuration"]["descriptor"]["annotations"] = {"unrelated": "yes"}
    calls = reply(monkeypatch, engine, [row])
    assert engine.images() == [
        Image("docker.io/library/ours:latest", "sha256:index", labels, ("sha256:manifest",), 2048)
    ]
    assert calls == [("image", "list", "--format", "json")]


def test_apple_mixed_platform_ownership_is_not_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AppleEngine()
    row = apple_image({MANAGED: "1"})
    row["variants"].extend(apple_image()["variants"])
    reply(monkeypatch, engine, [row])
    assert engine.images()[0].labels == {}


def test_apple_container_images_include_immutable_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AppleEngine()
    row = {
        "configuration": {
            "image": {"reference": "ours:latest", "descriptor": {"digest": "sha256:index"}}
        }
    }
    calls = reply(monkeypatch, engine, [row])
    assert engine.container_images() == {"ours:latest", "sha256:index"}
    assert calls == [("list", "--all", "--format", "json")]


@pytest.mark.parametrize("payload", [None, {}, [None], [{}], [{"configuration": {}}]])
@pytest.mark.parametrize("method", ["images", "container_images"])
def test_apple_cleanup_rejects_unknown_inventory(
    monkeypatch: pytest.MonkeyPatch, payload: object, method: str
) -> None:
    engine = AppleEngine()
    reply(monkeypatch, engine, payload)
    with pytest.raises(DevboxError, match="refusing cleanup"):
        getattr(engine, method)()


def test_podman_images_include_dangling_and_all_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = PodmanEngine()
    calls = []
    labels = {MANAGED: "1"}

    def call(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[1] == "list":
            rows = [{"Id": "sha256:tagged"}, {"Id": "sha256:dangling"}]
        else:
            id = args[2]
            tags = (
                ["localhost/ours:latest", "localhost/alias:latest"]
                if id.endswith("tagged")
                else None
            )
            rows = [
                {
                    "Id": id,
                    "RepoTags": tags,
                    "RepoDigests": [],
                    "Config": {"Labels": labels},
                    "Size": 4096,
                }
            ]
        return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")

    monkeypatch.setattr(engine, "call", call)
    images = engine.images()
    assert [image.reference for image in images] == [
        "localhost/ours:latest",
        "localhost/alias:latest",
        "sha256:dangling",
    ]
    assert images[0].aliases == ("localhost/ours:latest", "localhost/alias:latest")
    assert all(image.labels == labels for image in images)
    assert all(image.size == 4096 for image in images)
    assert calls[0] == ("image", "list", "--all", "--no-trunc", "--format", "json")


def test_podman_container_images_include_unmanaged_stopped_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PodmanEngine()
    calls = reply(
        monkeypatch,
        engine,
        [
            {
                "Names": ["other"],
                "State": "exited",
                "Image": "localhost/ours:latest",
                "ImageID": "immutable",
            }
        ],
    )
    assert engine.container_images() == {"localhost/ours:latest", "immutable"}
    assert calls == [("ps", "--all", "--no-trunc", "--format", "json")]


@pytest.mark.parametrize("payload", [None, {}, [None], [{}], [{"Id": ""}]])
@pytest.mark.parametrize("method", ["images", "container_images"])
def test_podman_cleanup_rejects_unknown_inventory(
    monkeypatch: pytest.MonkeyPatch, payload: object, method: str
) -> None:
    engine = PodmanEngine()
    reply(monkeypatch, engine, payload)
    with pytest.raises(DevboxError, match="refusing cleanup"):
        getattr(engine, method)()


@pytest.mark.parametrize("engine, command", [(AppleEngine(), "delete"), (PodmanEngine(), "rm")])
def test_image_delete_is_explicit_and_never_forced(
    monkeypatch: pytest.MonkeyPatch, engine: AppleEngine | PodmanEngine, command: str
) -> None:
    calls = reply(monkeypatch, engine, [])
    engine.remove_image("ours:latest")
    assert calls == [("image", command, "--", "ours:latest")]


@pytest.mark.parametrize("config", [None, {}, {"Labels": None}, {"Labels": {}}])
def test_optional_oci_config_is_not_ownership(config: object) -> None:
    assert image_labels(config) == {}


@pytest.mark.parametrize(
    "config", [[], "bad", {"Labels": []}, {"Labels": False}, {"Labels": {MANAGED: 1}}]
)
def test_malformed_oci_labels_fail_closed(config: object) -> None:
    with pytest.raises(ValueError):
        image_labels(config)


def test_references_are_rechecked_between_deletions(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    first = owned(app, "first:latest", "sha256:first")
    second = owned(app, "second:latest", "sha256:second")
    engine.inventory = [first, second]
    remove = engine.remove_image

    def remove_then_retain(reference: str) -> None:
        remove(reference)
        retain(app, second.reference)

    monkeypatch.setattr(engine, "remove_image", remove_then_retain)
    assert app.cleanup_images([first, second]) == (1, {image_identity(first.id): first.size})
    assert engine.deleted == [first.reference]
    assert engine.inventory == [second]


def test_image_recovery_counts_each_deleted_id_once(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    first = owned(app)
    alias = replace(first, reference="alias:latest")
    second = replace(owned(app, "second:latest", "sha256:second"), size=8192)
    engine.inventory = [first, alias, second]
    assert app.cleanup_images([first, first, alias, second]) == (
        3,
        {image_identity(first.id): 4096, image_identity(second.id): 8192},
    )
    assert engine.inventory == []


def test_image_recovery_uses_rechecked_sizes(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    candidates = app.unused_images()
    engine.inventory = [replace(image, size=8192)]
    assert app.cleanup_images(candidates) == (1, {image_identity(image.id): 8192})


def test_image_recovery_with_unknown_size_is_not_reported_as_zero(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    engine.inventory = [replace(owned(app), size=None)]
    patch_require_terminal(monkeypatch)
    patch_input(monkeypatch)
    dispatch(parser().parse_args(["cleanup"]), app)
    text = capsys.readouterr().out
    assert "Removed 1 image references; approximate recovered space unavailable." in text


def test_image_recovery_reports_known_sizes_beside_unknown_ones(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    engine.inventory = [replace(owned(app), size=None), owned(app, "known:latest", "sha256:known")]
    patch_require_terminal(monkeypatch)
    patch_input(monkeypatch)
    dispatch(parser().parse_args(["cleanup"]), app)
    text = capsys.readouterr().out
    assert (
        "Removed 2 image references; approximately 4.0 KiB recovered, plus images of unknown size."
        in text
    )


def test_apple_image_size_includes_all_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AppleEngine()
    row = apple_image()
    row["variants"].append({**row["variants"][0], "digest": "sha256:other", "size": 4096})
    reply(monkeypatch, engine, [row])
    assert engine.images()[0].size == 6144


@pytest.mark.parametrize("value", [None, -1, True, "4096", 1.5, {}])
@pytest.mark.parametrize("backend", ["apple", "podman"])
def test_optional_image_size_metadata_can_be_unavailable(
    monkeypatch: pytest.MonkeyPatch, backend: str, value: object
) -> None:
    engine: AppleEngine | PodmanEngine
    if backend == "apple":
        engine = AppleEngine()
        row = apple_image()
        row["variants"][0]["size"] = value
    else:
        engine = PodmanEngine()
        row = {
            "Id": "sha256:ours",
            "Config": {"Labels": {MANAGED: "1"}},
            "Size": value,
        }
    reply(monkeypatch, engine, [row])
    assert engine.images()[0].size is None


def test_inventory_failure_does_not_delete_instance_homes(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    id = retain(app, "missing:latest")

    def fail() -> list[Image]:
        raise DevboxError("Cannot safely determine existing images; refusing cleanup")

    patch_require_terminal(monkeypatch)
    monkeypatch.setattr(engine, "images", fail)
    monkeypatch.setattr(
        "builtins.input", lambda _: pytest.fail("must not confirm incomplete inventory")
    )
    with pytest.raises(DevboxError, match="refusing cleanup"):
        dispatch(parser().parse_args(["cleanup"]), app)
    assert app.store.directory(id).exists()
    assert engine.deleted == []


def test_container_inventory_failure_never_deletes_images(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    candidates = app.unused_images()

    def fail() -> set[str]:
        raise DevboxError("Cannot safely determine container images; refusing cleanup")

    monkeypatch.setattr(engine, "container_images", fail)
    with pytest.raises(DevboxError, match="refusing cleanup"):
        app.cleanup_images(candidates)
    assert engine.deleted == []


def test_image_became_referenced_during_confirmation_is_skipped(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    patch_require_terminal(monkeypatch)

    def confirm(prompt: str) -> str:
        retain(app, image.reference)
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    dispatch(parser().parse_args(["cleanup"]), app)
    assert engine.deleted == []


@pytest.mark.parametrize(
    ("sizes", "estimate"),
    [
        ((4096, 8192), "~12.0 KiB"),
        ((4096, None), ">4.0 KiB"),
        ((None, None), "size unavailable"),
        ((), "~0.0 B"),
    ],
)
def test_cleanup_confirmation_totals_image_sizes(
    app: App,
    sizes: tuple[int | None, ...],
    estimate: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    images = [
        replace(owned(app, f"image{n}:latest", f"sha256:image{n}"), size=size)
        for n, size in enumerate(sizes)
    ]
    # Another tag of the first image must not count its bytes twice.
    engine.inventory = [
        *images,
        *[replace(image, reference="alias:latest") for image in images[:1]],
    ]
    _, directory = saved_instance(app.store)
    instance_size = format_size(disk_usage(directory))
    patch_require_terminal(monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    dispatch(parser().parse_args(["cleanup"]), app)
    count = len(engine.inventory)
    assert prompts == [
        f"Delete 1 instances (~{instance_size}) and {count} images ({estimate})? [y/N] "
    ]
    assert engine.deleted == []


def test_cleanup_only_instances_leaves_images_alone(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    monkeypatch.setattr(app, "unused_images", lambda *a: pytest.fail("images are out of scope"))
    id = retain(app, image.reference)
    size = format_size(disk_usage(app.store.directory(id)))
    patch_require_terminal(monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    dispatch(parser().parse_args(["cleanup", "--only", "instances"]), app)
    assert prompts == [f"Delete 1 instances (~{size})? [y/N] "]
    assert not (app.store.instances / id).exists()
    assert engine.deleted == []
    text = capsys.readouterr().out
    assert "Removed 1 instances" in text
    assert "image" not in text.lower()


def test_cleanup_only_images_keeps_instances_and_their_images(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    unused = owned(app)
    kept = owned(app, "kept:latest", "sha256:kept")
    engine.inventory = [unused, kept]
    id = retain(app, kept.reference)
    patch_require_terminal(monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    dispatch(parser().parse_args(["cleanup", "--only", "images"]), app)
    assert prompts == ["Delete 1 images (~4.0 KiB)? [y/N] "]
    assert engine.deleted == [unused.reference]
    assert app.store.directory(id).exists()
    text = capsys.readouterr().out
    assert "Orphaned instances" not in text
    assert "Removed 1 image references; approximately 4.0 KiB recovered." in text
    assert "Removed 1 instances" not in text
    assert "shared layers and build caches may reduce recovery" in text


@pytest.mark.parametrize(
    ("only", "message"),
    [("instances", "No orphaned instances.\n"), ("images", "No unused Devbox images.\n")],
)
def test_cleanup_only_reports_empty_scope(
    app: App,
    only: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    # Whatever the other scope holds must not be offered.
    if only == "instances":
        engine.inventory = [owned(app)]
    else:
        saved_instance(app.store)
    patch_require_terminal(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("nothing to confirm"))
    dispatch(parser().parse_args(["cleanup", "--only", only]), app)
    assert capsys.readouterr().out == message
    assert engine.deleted == []


@pytest.mark.parametrize(
    ("only", "offered"),
    # Images freed by chosen instances are offered; --only images keeps every instance.
    [(None, ["chosen:latest", "free:latest"]), ("images", ["free:latest"])],
)
def test_cleanup_select_offers_images_after_instances(
    app: App,
    only: str | None,
    offered: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    free = owned(app, "free:latest", "sha256:free")
    chosen = owned(app, "chosen:latest", "sha256:chosen")
    kept = replace(owned(app, "kept:latest", "sha256:kept"), size=None)
    engine.inventory = [free, chosen, kept]
    chosen_id = retain(app, chosen.reference)
    kept_id = retain(app, kept.reference)
    patch_require_terminal(monkeypatch)
    pickers = []

    def pick(rows: list, prompt: str, label: Callable[[object], str]) -> list:
        pickers.append((prompt, [label(row) for row in rows]))
        if prompt == "Orphaned instances":
            return [row for row in rows if row.id == chosen_id]
        assert [row.reference for row in rows] == offered
        return rows[-1:]

    prompts = []
    monkeypatch.setattr("devbox.cli.select_many", pick)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "y")
    dispatch(parser().parse_args(["cleanup", "--select", *(["--only", only] if only else [])]), app)
    assert [prompt for prompt, _ in pickers] == (
        ["Unused images"] if only else ["Orphaned instances", "Unused images"]
    )
    assert pickers[-1][1][-1] == "free:latest  ~4.0 KiB"
    assert engine.deleted == ["free:latest"]
    assert (app.store.instances / kept_id).exists()
    assert (app.store.instances / chosen_id).exists() == bool(only)
    if only:
        assert prompts == ["Delete 1 images (~4.0 KiB)? [y/N] "]
    else:
        assert prompts[0].endswith("and 1 images (~4.0 KiB)? [y/N] ")
    assert "Removed 1 image references" in capsys.readouterr().out


def test_cleanup_select_with_nothing_picked_removes_nothing(
    app: App, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    engine.inventory = [owned(app)]
    id = retain(app, "other:latest")
    patch_require_terminal(monkeypatch)
    monkeypatch.setattr("devbox.cli.select_many", lambda *a: [])
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("nothing to confirm"))
    dispatch(parser().parse_args(["cleanup", "--select"]), app)
    assert capsys.readouterr().out == "Nothing selected. Nothing removed.\n"
    assert engine.deleted == []
    assert (app.store.instances / id).exists()


def test_image_sizes_exclude_base_image(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    project = replace(
        owned(app, "devbox-project-x:latest", "sha256:project"),
        labels={**owned(app).labels, IMAGE_KIND: "project", IMAGE_BASE: "ubuntu:24.04"},
        size=10000,
    )
    prepared = replace(
        owned(app, "devbox-prepared-x:latest", "sha256:prepared"),
        labels={**owned(app).labels, IMAGE_BASE: "devbox-project-x:latest"},
        size=12000,
    )
    base = Image("docker.io/library/ubuntu:24.04", "sha256:ubuntu", {}, size=8000)
    engine.inventory = [base, project, prepared]
    assert {image.reference: image.size for image in app.unused_images()} == {
        "devbox-prepared-x:latest": 2000,
        "devbox-project-x:latest": 2000,
    }
    # An unresolved base leaves the full size.
    engine.inventory = [prepared]
    assert [image.size for image in app.unused_images()] == [12000]


def test_containerfile_base_resolves_final_stage(tmp_path: Path) -> None:
    file = tmp_path / "Containerfile"
    file.write_text("ARG V=24.04\nFROM --platform=x ubuntu:${V} AS a\nFROM a\nRUN true\n")
    assert containerfile_base(file) == "ubuntu:24.04"
    file.write_text("ARG IMG\nFROM ${IMG}\n")
    assert containerfile_base(file) == ""
