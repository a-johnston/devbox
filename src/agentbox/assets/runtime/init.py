#!/usr/bin/python3
"""Initialize the guest, then keep its lifetime independent of terminal clients."""

import json
import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    Path("/run/agentbox-ready").unlink(missing_ok=True)
    config = json.loads(Path("/etc/agentbox/project.json").read_text())
    for key, value in config.get("git_identity", {}).items():
        subprocess.run(["/usr/bin/git", "config", "--system", key, value], check=True)
    bin_dir = Path("/opt/agentbox/bin")
    bin_dir.mkdir(exist_ok=True)
    commands = {}
    for name in config["prepare_commands"]:
        search = os.environ["PATH"].replace("/opt/agentbox/bin:", "")
        executable = shutil.which(name, path=search)
        if executable is None:
            raise SystemExit(f"Project prepare_commands executable not installed: {name}")
        commands[name] = executable
        (bin_dir / name).unlink(missing_ok=True)
        (bin_dir / name).symlink_to("/opt/agentbox/command.py")
    Path("/run/agentbox-commands.json").write_text(json.dumps(commands))
    subprocess.run(["/opt/agentbox/prepare.py"], check=True)
    Path("/run/agentbox-ready").touch()
    os.execv("/usr/bin/sleep", ["sleep", "infinity"])


if __name__ == "__main__":
    main()
