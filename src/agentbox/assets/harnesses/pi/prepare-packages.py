#!/usr/bin/python3
"""Bake selected Linux packages into a seed, without host settings or auth."""

import json
import os
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    seed = Path("/opt/agentbox/prepared/.pi/agent")
    seed.mkdir(parents=True, exist_ok=True)
    packages = json.loads(Path("/opt/agentbox/pi-build/package-sources.json").read_text())
    with tempfile.TemporaryDirectory(prefix="agentbox-npm-") as cache:
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(seed), "npm_config_cache": cache}
        for source in packages:
            subprocess.run(["pi", "install", source], env=env, check=True)
    for directory in ("npm", "git"):
        (seed / directory).mkdir(exist_ok=True)
    # cp -a in the root preparation helper preserves the guest agent's ownership.
    subprocess.run(["chown", "-R", "10001:10001", str(seed)], check=True)


if __name__ == "__main__":
    main()
