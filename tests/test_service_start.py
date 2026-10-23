"""Service startup uses fake subprocesses, never the host's container service."""

import subprocess

import pytest

from devbox.engine.apple import AppleEngine
from devbox.engine.podman import PodmanEngine
from devbox.errors import DevboxError


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict]]:
    calls: list[tuple[list[str], dict]] = []

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args == ["container", "system", "status"]:
            running = any(argv == ["container", "system", "start"] for argv, _ in calls)
            return subprocess.CompletedProcess(
                args, 0, "apiserver is running" if running else "apiserver is not running", ""
            )
        assert args == ["container", "system", "start"]
        return subprocess.CompletedProcess(args, 0, None, None)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    return calls


@pytest.mark.parametrize("answer", ["y", "yes", " Y ", " YES "])
def test_confirmed_start_rechecks_service(
    monkeypatch: pytest.MonkeyPatch, service: list, answer: str
) -> None:
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    AppleEngine().require_running()
    assert len(prompts) == 1
    assert "[y/N]" in prompts[0]
    assert [args for args, _ in service] == [
        ["container", "system", "status"],
        ["container", "system", "start"],
        ["container", "system", "status"],
    ]
    assert service[1][1]["capture_output"] is False


@pytest.mark.parametrize("answer", ["", "n", "no", "anything else"])
def test_start_defaults_to_no(monkeypatch: pytest.MonkeyPatch, service: list, answer: str) -> None:
    monkeypatch.setattr("builtins.input", lambda _: answer)
    with pytest.raises(DevboxError, match="container system start"):
        AppleEngine().require_running()
    assert len(service) == 1


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_noninteractive_start_never_prompts(
    monkeypatch: pytest.MonkeyPatch, service: list, stream: str
) -> None:
    monkeypatch.setattr(f"sys.{stream}.isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    with pytest.raises(DevboxError, match="container system start"):
        AppleEngine().require_running()
    assert len(service) == 1


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_start_cancellation(
    monkeypatch: pytest.MonkeyPatch, service: list, error: type[BaseException]
) -> None:
    def cancel(_: str) -> str:
        raise error

    monkeypatch.setattr("builtins.input", cancel)
    expected = DevboxError if error is EOFError else KeyboardInterrupt
    with pytest.raises(expected):
        AppleEngine().require_running()
    assert len(service) == 1


def test_running_service_never_prompts(monkeypatch: pytest.MonkeyPatch, service: list) -> None:
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        service.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "apiserver is running", "")

    monkeypatch.setattr(subprocess, "run", run)
    AppleEngine().require_running()
    assert [args for args, _ in service] == [["container", "system", "status"]]


@pytest.mark.parametrize(
    "start_code,status_code,status_out,status_err,expected",
    [
        (1, 0, "", "", "container system failed: See output above"),
        (0, 0, "apiserver is not running", "", "still unavailable.*not running"),
        (0, 1, "", "status check failed", "still unavailable.*status check failed"),
    ],
)
def test_start_failures_stop_execution(
    monkeypatch: pytest.MonkeyPatch,
    service: list,
    start_code: int,
    status_code: int,
    status_out: str,
    status_err: str,
    expected: str,
) -> None:
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        service.append((args, kwargs))
        if args == ["container", "system", "start"]:
            return subprocess.CompletedProcess(args, start_code, None, None)
        if len(service) == 1:
            return subprocess.CompletedProcess(args, 1, "", "apiserver is not running")
        return subprocess.CompletedProcess(args, status_code, status_out, status_err)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    with pytest.raises(DevboxError, match=expected):
        AppleEngine().require_running()
    assert len(service) == (2 if start_code else 3)


def test_missing_cli_never_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    with pytest.raises(DevboxError, match="Apple's container CLI is required"):
        AppleEngine().require_running()


def test_podman_failure_never_prompts(monkeypatch: pytest.MonkeyPatch, service: list) -> None:
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        service.append((args, kwargs))
        return subprocess.CompletedProcess(args, 125, "", "storage is broken")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(DevboxError, match="podman info failed: storage is broken"):
        PodmanEngine().require_running()
    assert [args for args, _ in service] == [["podman", "info", "--format", "json"]]
