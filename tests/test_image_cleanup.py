import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from devbox.app import App
from devbox.apple import AppleEngine
from devbox.cli import dispatch, parser
from devbox.config import Harness, Project
from devbox.engine import IMAGE_KIND, IMAGE_SCHEMA, IMAGE_STORE, MANAGED, Image, image_labels
from devbox.errors import DevboxError
from devbox.podman import PodmanEngine
from devbox.state import Store
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
    return Image(reference, id, {**app.image_owner, IMAGE_KIND: "prepared"})


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
    assert app.cleanup_images([image, image, foreign_store, legacy, future, unknown]) == 1
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
    assert app.cleanup_images([alias, saved, running, stopped, unused]) == 1
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
    assert app.cleanup_images(candidates) == 0
    assert engine.deleted == []


def test_cleanup_never_adds_new_image_candidates(app: App) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    candidates = app.unused_images()
    newer = owned(app, "newer:latest", "sha256:newer")
    engine.inventory.append(newer)
    assert app.cleanup_images(candidates) == 1
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


@pytest.mark.parametrize("select", [False, True])
def test_image_only_cleanup_still_confirms(
    app: App, select: bool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    patch_require_terminal(monkeypatch)
    monkeypatch.setattr("devbox.cli.select_many", lambda *a: pytest.fail("no instances to pick"))

    def confirm(prompt: str) -> str:
        assert image.reference in capsys.readouterr().out
        assert "1 image" in prompt
        assert not engine.deleted
        return "y"

    monkeypatch.setattr("builtins.input", confirm)
    dispatch(parser().parse_args(["cleanup", *(["--select"] if select else [])]), app)
    assert engine.deleted == [image.reference]
    assert "Removed 1 image references." in capsys.readouterr().out
    assert not app.store.root.exists()


def test_combined_cleanup_removes_images_after_selected_homes(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    image = owned(app)
    engine.inventory = [image]
    retain(app, image.reference)
    patch_require_terminal(monkeypatch)
    patch_input(monkeypatch)
    dispatch(parser().parse_args(["cleanup"]), app)
    assert engine.deleted == [image.reference]
    assert list(app.store.instances.iterdir()) == []


def test_select_keeps_unselected_sessions_and_their_images(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    first = owned(app)
    kept = owned(app, "kept:latest", "sha256:kept")
    engine.inventory = [first, kept]
    selected = retain(app, first.reference)
    retained = retain(app, kept.reference)
    patch_require_terminal(monkeypatch)
    monkeypatch.setattr(
        "devbox.cli.select_many", lambda rows, *a: [row for row in rows if row.id == selected]
    )
    patch_input(monkeypatch)
    dispatch(parser().parse_args(["cleanup", "--select"]), app)
    assert engine.deleted == [first.reference]
    assert app.store.directory(retained).exists()


def test_image_cleanup_cancellation_deletes_nothing(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = app.engine
    assert isinstance(engine, ImageEngine)
    engine.inventory = [owned(app)]
    patch_require_terminal(monkeypatch)
    patch_input(monkeypatch, "n")
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
        Image("docker.io/library/ours:latest", "sha256:index", labels, ("sha256:manifest",))
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
            rows = [{"Id": id, "RepoTags": tags, "RepoDigests": [], "Config": {"Labels": labels}}]
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
    assert app.cleanup_images([first, second]) == 1
    assert engine.deleted == [first.reference]
    assert engine.inventory == [second]


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
