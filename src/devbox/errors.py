"""User-facing errors, without tracebacks or credential-bearing command dumps."""


class DevboxError(Exception):
    """An actionable configuration or lifecycle error."""
