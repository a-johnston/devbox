"""Run the privileged guest setup directly, with a temporary sudoers directory."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from devbox.config import ASSETS

POLICY = "agent ALL=(ALL:ALL) NOPASSWD: ALL\n"
SCRIPT = ASSETS / "runtime/allow-root.sh"


@pytest.mark.parametrize("valid", [False, True])
def test_root_policy_is_validated_and_installed_atomically(valid: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub only the external validator: the script's file writes, permissions,
    # replacement and cleanup run for real without needing guest root on the host.
    tools = tmp_path / "bin"
    tools.mkdir()
    validator = tools / "visudo"
    validator.write_text(
        "#!/bin/sh\nset -eu\n"
        '[ "$1" = "-cf" ]\n'
        'test "$(cat "$2")" = "agent ALL=(ALL:ALL) NOPASSWD: ALL"\n'
        'cp "$2" "$VALIDATED_POLICY"\n'
        f"exit {0 if valid else 1}\n"
    )
    validator.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("VALIDATED_POLICY", str(tmp_path / "validated"))
    directory = tmp_path / "sudoers.d"
    directory.mkdir()
    destination = directory / "devbox-agent"
    destination.write_text("previous policy\n")
    result = subprocess.run(["sh", "-s", "--", str(directory)], input=SCRIPT.read_text(), capture_output=True, text=True)
    assert result.returncode == (0 if valid else 1), result.stderr
    assert (tmp_path / "validated").read_text() == POLICY
    assert destination.read_text() == (POLICY if valid else "previous policy\n")
    assert list(directory.iterdir()) == [destination]
    if valid:
        assert stat.S_IMODE(destination.stat().st_mode) == 0o440


def test_root_policy_syntax() -> None:
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo is not available on this host")
    result = subprocess.run([visudo, "-cf", "-"], input=POLICY, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
