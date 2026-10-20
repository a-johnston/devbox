"""User-facing errors, without tracebacks or credential-bearing command dumps."""


class AgentboxError(Exception):
    """An actionable configuration or lifecycle error."""
