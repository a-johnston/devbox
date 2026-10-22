import pytest


def patch_require_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("devbox.cli.require_terminal", lambda *a: None)


def patch_input(monkeypatch: pytest.MonkeyPatch, mock_input: str = "y") -> None:
    monkeypatch.setattr("builtins.input", lambda _: mock_input)
