#!/usr/bin/python3
"""Execute the harness in tmux; no shell interpolation of user-supplied arguments."""

import json
import os
from pathlib import Path


def main() -> None:
    config = json.loads(Path("/etc/agentbox/project.json").read_text())
    os.chdir(config["cwd"])
    command = config["command"]
    os.execvpe(command[0], command, dict(os.environ))


if __name__ == "__main__":
    main()
