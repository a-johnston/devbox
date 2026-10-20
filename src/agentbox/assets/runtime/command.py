#!/usr/bin/python3
"""Prepare caches before build commands and after git worktree add."""

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    subprocess.run(["/usr/bin/sudo", "-n", "/opt/agentbox/prepare.py"], check=True)
    name = Path(sys.argv[0]).name
    commands = json.loads(Path("/run/agentbox-commands.json").read_text())
    executable = commands[name]
    args = [executable, *sys.argv[1:]]
    if name == "git" and "worktree" in args and "add" in args:
        result = subprocess.run(args, check=False)
        # A post-checkout hook may fail after Git has already created the worktree.
        subprocess.run(["/usr/bin/sudo", "-n", "/opt/agentbox/prepare.py"], check=True)
        raise SystemExit(result.returncode)
    os.execv(executable, args)


if __name__ == "__main__":
    main()
