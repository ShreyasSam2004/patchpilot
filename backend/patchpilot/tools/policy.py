"""Tool permission policy.

The separation this enforces is the point of the whole design:

* an **MCP server** decides which tools *exist*;
* **PatchPilot** decides which tools this agent is *allowed* to call;
* the **model** decides which allowed tool to call *now*.

Deny always wins over allow. Anything not on the allow-list is refused, so a
newly discovered MCP tool cannot become reachable by accident.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from ..logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


@dataclass
class ToolPolicy:
    """Default-deny allow/deny lists supporting ``fnmatch`` patterns."""

    allow: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    default_allow: bool = False
    """Only ever True for tests; production configuration is default-deny."""

    def decide(self, name: str) -> PolicyDecision:
        for pattern in self.deny:
            if _matches(name, pattern):
                return PolicyDecision(False, f"denied by policy rule {pattern!r}")
        if self.default_allow:
            return PolicyDecision(True, "default-allow policy")
        for pattern in self.allow:
            if _matches(name, pattern):
                return PolicyDecision(True, f"allowed by policy rule {pattern!r}")
        return PolicyDecision(False, "not present in the allow-list (default deny)")

    def permits(self, name: str) -> bool:
        return self.decide(name).allowed

    def describe(self) -> dict[str, object]:
        return {
            "allow": list(self.allow),
            "deny": list(self.deny),
            "default": "allow" if self.default_allow else "deny",
        }

    @classmethod
    def from_settings(cls, settings) -> ToolPolicy:
        return cls(allow=list(settings.tool_allow), deny=list(settings.tool_deny))

    @classmethod
    def permissive(cls) -> ToolPolicy:
        """Test-only policy. Never constructed from configuration."""
        return cls(allow=["*"], deny=[], default_allow=True)


def _matches(name: str, pattern: str) -> bool:
    if pattern == name:
        return True
    return fnmatch.fnmatchcase(name, pattern)
