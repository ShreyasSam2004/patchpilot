"""Typed errors.

Errors are split into two families:

* :class:`ToolError` — recoverable. The agent sees the message as an
  observation and is expected to try something else.
* :class:`PatchPilotError` subclasses that are *not* tool errors — these abort
  the run and surface as ``RUN_FAILED``.
"""

from __future__ import annotations


class PatchPilotError(Exception):
    """Base class for all PatchPilot errors."""


class ConfigurationError(PatchPilotError):
    """Invalid or missing configuration (bad URL, absent API key, ...)."""


class WorkspaceError(PatchPilotError):
    """Repository could not be prepared."""


class SandboxError(PatchPilotError):
    """The sandbox itself failed (daemon down, image missing, ...)."""


class ProviderError(PatchPilotError):
    """Model provider failed in a way the agent cannot recover from."""


class BudgetExceeded(PatchPilotError):
    """A configured limit was reached; the run stops with TIMEOUT."""

    def __init__(self, limit: str, detail: str = "") -> None:
        self.limit = limit
        super().__init__(f"budget exceeded: {limit}{f' ({detail})' if detail else ''}")


class ToolError(PatchPilotError):
    """Recoverable failure inside a tool; reported back to the model."""

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        self.code = code
        super().__init__(message)


class ToolNotFound(ToolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown tool {name!r}", code="tool_not_found")


class ToolNotPermitted(ToolError):
    def __init__(self, name: str, reason: str) -> None:
        super().__init__(f"tool {name!r} is not permitted: {reason}", code="tool_denied")


class ToolValidationError(ToolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="invalid_arguments")


class ToolTimeout(ToolError):
    def __init__(self, name: str, timeout_s: float) -> None:
        super().__init__(f"tool {name!r} timed out after {timeout_s:.0f}s", code="timeout")


class PathEscapeError(ToolError):
    """A path resolved outside the workspace jail."""

    def __init__(self, path: str) -> None:
        super().__init__(
            f"path {path!r} resolves outside the workspace and was refused",
            code="path_escape",
        )


class BenchmarkError(PatchPilotError):
    """SWE-bench dataset loading or evaluation failed."""
