"""Tool registry.

Holds every tool the agent might use, filters them through the
:class:`~patchpilot.tools.policy.ToolPolicy`, and dispatches calls. Native tools
and MCP tools sit in the same registry and are indistinguishable to the agent.

Name collisions between a native tool and an MCP tool are resolved in favour of
the native implementation, and the MCP one is re-registered under its
``mcp__<server>__<tool>`` qualified name so it stays reachable but never
shadows a trusted local implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..domain.enums import ToolOrigin
from ..errors import ToolNotFound, ToolNotPermitted
from ..logging_setup import get_logger
from .base import Tool, ToolContext, ToolResult, ToolSpec
from .policy import ToolPolicy

log = get_logger(__name__)


@dataclass
class RegisteredTool:
    tool: Tool
    origin: ToolOrigin
    server: str | None = None
    allowed: bool = True
    policy_reason: str = ""


@dataclass
class ToolRegistry:
    """Policy-filtered collection of tools."""

    policy: ToolPolicy = field(default_factory=ToolPolicy)
    _tools: dict[str, RegisteredTool] = field(default_factory=dict)

    # ------------------------------------------------------------- mutation
    def register(self, tool: Tool, *, server: str | None = None) -> str:
        """Register a tool, returning the name it is reachable under."""
        name = tool.name
        existing = self._tools.get(name)
        if existing is not None:
            if existing.origin is ToolOrigin.NATIVE and tool.origin is ToolOrigin.MCP:
                qualified = f"mcp__{server or 'server'}__{name}"
                log.warning(
                    "tools.collision", name=name, resolved_as=qualified, kept="native"
                )
                return self._insert(qualified, tool, server)
            log.warning("tools.replaced", name=name)
        return self._insert(name, tool, server)

    def _insert(self, name: str, tool: Tool, server: str | None) -> str:
        decision = self.policy.decide(name)
        self._tools[name] = RegisteredTool(
            tool=tool,
            origin=tool.origin,
            server=server,
            allowed=decision.allowed,
            policy_reason=decision.reason,
        )
        log.debug(
            "tools.registered",
            name=name,
            origin=str(tool.origin),
            allowed=decision.allowed,
            reason=decision.reason,
        )
        return name

    def register_all(self, tools: list[Tool], *, server: str | None = None) -> None:
        for tool in tools:
            self.register(tool, server=server)

    # -------------------------------------------------------------- queries
    def get(self, name: str) -> Tool:
        entry = self._tools.get(name)
        if entry is None:
            raise ToolNotFound(name)
        if not entry.allowed:
            raise ToolNotPermitted(name, entry.policy_reason)
        return entry.tool

    def has(self, name: str) -> bool:
        entry = self._tools.get(name)
        return entry is not None and entry.allowed

    @property
    def allowed_names(self) -> list[str]:
        return sorted(name for name, e in self._tools.items() if e.allowed)

    @property
    def blocked_names(self) -> list[str]:
        return sorted(name for name, e in self._tools.items() if not e.allowed)

    def specs(self) -> list[ToolSpec]:
        """Schemas for the *allowed* tools, in a stable order."""
        return [
            self._tools[name].tool.spec()
            for name in self.allowed_names
        ]

    def inventory(self) -> list[dict[str, Any]]:
        """Full inventory including blocked tools — for the settings UI."""
        rows = []
        for name in sorted(self._tools):
            entry = self._tools[name]
            rows.append(
                {
                    "name": name,
                    "description": entry.tool.description,
                    "origin": str(entry.origin),
                    "server": entry.server,
                    "allowed": entry.allowed,
                    "reason": entry.policy_reason,
                    "mutating": entry.tool.mutating,
                }
            )
        return rows

    # ------------------------------------------------------------- dispatch
    async def call(self, name: str, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        """Dispatch a model-requested tool call, never raising for policy issues."""
        try:
            tool = self.get(name)
        except ToolNotFound:
            available = ", ".join(self.allowed_names)
            return ToolResult.failure(
                f"unknown tool {name!r}. Available tools: {available}",
                code="tool_not_found",
            )
        except ToolNotPermitted as exc:
            return ToolResult.failure(str(exc), code="tool_denied")
        return await tool.run(ctx, args)

    def reapply_policy(self, policy: ToolPolicy) -> None:
        """Swap the policy and re-evaluate every registered tool."""
        self.policy = policy
        for name, entry in self._tools.items():
            decision = policy.decide(name)
            entry.allowed = decision.allowed
            entry.policy_reason = decision.reason
