from pathlib import Path

import pytest

from devbox.app import App
from devbox.apple import AppleEngine
from devbox.errors import DevboxError
from devbox.podman import PodmanEngine
from devbox.state import Store


@pytest.mark.parametrize("engine_type", [AppleEngine, PodmanEngine])
def test_default_store_follows_injected_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_type: type
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    app = App(engine_type())
    assert app.store.root == tmp_path / "devbox" / app.engine.name


@pytest.mark.parametrize("engine_type,backend", [(AppleEngine, "podman"), (PodmanEngine, "apple")])
def test_app_rejects_mismatched_state_backend(
    tmp_path: Path, engine_type: type, backend: str
) -> None:
    with pytest.raises(DevboxError, match="saved-state backend must match"):
        App(engine_type(), Store(tmp_path, backend=backend))


def test_image_ownership_is_scoped_by_backend(tmp_path: Path) -> None:
    apple = App(AppleEngine(), Store(tmp_path, backend="apple"))
    podman = App(PodmanEngine(), Store(tmp_path, backend="podman"))
    assert apple.image_owner != podman.image_owner
