import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from devbox.config import ASSETS


@pytest.mark.skipif(shutil.which("node") is None, reason="Pi launcher tests require Node.js")
def test_pi_launcher() -> None:
    tests = Path(__file__).parent / "pi/launcher.test.mjs"
    result = subprocess.run(["node", "--test", str(tests)], capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Pi launcher tests require Node.js")
def test_pi_launcher_help_does_not_need_an_installed_sdk() -> None:
    launcher = ASSETS / "harnesses/pi/launcher.mjs"
    result = subprocess.run(["node", str(launcher), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "DEVBOX_SHARED_AUTH" in result.stdout


@pytest.mark.skipif(not os.environ.get("DEVBOX_PI_SDK"), reason="Opt-in installed Pi SDK check")
def test_pi_interactive_login_selector(tmp_path: Path) -> None:
    import fcntl
    import pty
    import select
    import struct
    import termios
    import time

    # Reproduce the image's package resolution without modifying the installed Pi.
    sdk = Path(os.environ["DEVBOX_PI_SDK"]).resolve()
    modules = tmp_path / "node_modules/@earendil-works"
    modules.mkdir(parents=True)
    (modules / "pi-coding-agent").symlink_to(sdk.parent.parent, target_is_directory=True)
    launcher = tmp_path / "launcher.mjs"
    shutil.copy(ASSETS / "harnesses/pi/launcher.mjs", launcher)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    process = subprocess.Popen(
        ["node", str(launcher), "--offline", "--no-extensions"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PI_CODING_AGENT_DIR": str(tmp_path / "agent"),
            "PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "sessions"),
            "DEVBOX_SHARED_AUTH": str(tmp_path / "shared"),
            "PI_SKIP_VERSION_CHECK": "1",
            "TERM": "xterm-256color",
        },
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = bytearray()

    def wait_for(text: bytes) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
                plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", output)
                if text in plain:
                    return
            if process.poll() is not None:
                break
        pytest.fail(f"Pi did not display {text!r}: {output[-1000:].decode(errors='replace')}")

    try:
        wait_for(b"/ commands")
        os.write(master, b"/login\r")
        wait_for(b"Select authentication method:")
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        os.close(master)
