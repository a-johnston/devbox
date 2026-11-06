from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfoNotFoundError

import pytest

from devbox.app import App, host_timezone
from devbox.config import Harness, Project
from devbox.errors import DevboxError
from devbox.state import Store
from tests.fakes import FakeEngine, FakeGit


@pytest.mark.parametrize("name", ["America/Los_Angeles", "Asia/Kolkata", "Etc/UTC"])
def test_host_timezone_uses_the_detected_zone_name(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    detect = Mock(return_value=name)
    monkeypatch.setattr("devbox.app.get_localzone_name", detect)
    assert host_timezone() == name
    detect.assert_called_once_with()


@pytest.mark.parametrize("failure", [None, OSError("unreadable"), ValueError("invalid"), ZoneInfoNotFoundError("conflicting")])
def test_timezone_detection_failure_is_actionable(failure: Exception | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.app.get_localzone_name", Mock(return_value=None, side_effect=failure))
    with pytest.raises(DevboxError, match="Cannot determine the host timezone; set TZ"):
        host_timezone()


@pytest.mark.parametrize("no_config", [False, True])
def test_new_detects_timezone_per_instance_without_changing_the_image(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_config: bool
) -> None:
    monkeypatch.delenv("TZ", raising=False)
    detect = Mock(side_effect=["America/Los_Angeles", "Asia/Tokyo"])
    monkeypatch.setattr("devbox.app.host_timezone", detect)
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
    app.new(project, harness, no_config=no_config)
    app.new(project, harness, no_config=no_config)
    assert [spec.env["TZ"] for spec in engine.runs] == ["America/Los_Angeles", "Asia/Tokyo"]
    assert detect.call_count == 2
    assert engine.runs[0].image == engine.runs[1].image
    assert len(engine.builds) == 3
    assert all("TZ" not in build.args for build in engine.builds)


@pytest.mark.parametrize(
    ("host", "project_tz", "harness_tz", "expected"),
    [
        ("Europe/London", None, None, "Europe/London"),
        ("EST5EDT,M3.2.0,M11.1.0", None, None, "EST5EDT,M3.2.0,M11.1.0"),
        ("", None, None, ""),
        (None, "Europe/Paris", None, "Europe/Paris"),
        ("Europe/London", "Europe/Paris", None, "Europe/Paris"),
        ("Europe/London", "Europe/Paris", "Etc/UTC", "Etc/UTC"),
    ],
)
def test_explicit_timezone_uses_normal_environment_precedence(
    project: Project,
    harness: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host: str | None,
    project_tz: str | None,
    harness_tz: str | None,
    expected: str,
) -> None:
    if host is None:
        monkeypatch.delenv("TZ", raising=False)
    else:
        monkeypatch.setenv("TZ", host)
    detect = Mock(side_effect=AssertionError("Explicit TZ must not require host detection"))
    monkeypatch.setattr("devbox.app.host_timezone", detect)
    project = replace(project, env={"TZ": project_tz} if project_tz is not None else {})
    harness = replace(harness, env={"TZ": harness_tz} if harness_tz is not None else {})
    engine = FakeEngine()
    App(engine, Store(tmp_path / "state"), FakeGit()).new(project, harness, no_config=True)
    assert engine.runs[0].env["TZ"] == expected
    detect.assert_not_called()


def test_detection_failure_does_not_build_or_create_state(
    project: Project, harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr("devbox.app.host_timezone", Mock(side_effect=DevboxError("Cannot determine the host timezone")))
    engine = FakeEngine()
    app = App(engine, Store(tmp_path / "state"), FakeGit())
    with pytest.raises(DevboxError, match="Cannot determine the host timezone"):
        app.new(project, harness, no_config=True)
    assert not engine.builds and not engine.runs
    assert not app.store.instances.exists()
