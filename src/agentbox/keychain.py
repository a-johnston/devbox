"""Harness secrets in the macOS login Keychain, never in agentbox state or images."""

from __future__ import annotations

import re
import subprocess

from agentbox.config import Harness
from agentbox.errors import AgentboxError

SERVICE = "dev.agentbox"
# Values are quoted into a `security -i` command line, so keep them to token characters.
VALUE = re.compile(r"[A-Za-z0-9._~+/=-]+")


def security(*args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["security", *args], input=input, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise AgentboxError("Harness secrets require the macOS Keychain") from exc


def load(harness: Harness) -> dict[str, str]:
    values = {}
    for name in harness.secrets:
        result = security(
            "find-generic-password", "-s", SERVICE, "-a", f"{harness.name}/{name}", "-w"
        )
        if result.returncode == 0 and (value := result.stdout.strip()):
            values[name] = value
    return values


def store(harness: Harness, name: str, value: str) -> None:
    if not VALUE.fullmatch(value):
        raise AgentboxError(f"{name} must be a nonempty token without spaces or quotes")
    account = f"{harness.name}/{name}"
    # Interactive mode reads the command from stdin: the value never appears in process arguments.
    security("-i", input=f'add-generic-password -U -s {SERVICE} -a {account} -w "{value}"\n')
    if load(harness).get(name) != value:
        raise AgentboxError(f"Could not store {name} in the Keychain")


def remove(harness: Harness, name: str) -> bool:
    return (
        security(
            "delete-generic-password", "-s", SERVICE, "-a", f"{harness.name}/{name}"
        ).returncode
        == 0
    )
