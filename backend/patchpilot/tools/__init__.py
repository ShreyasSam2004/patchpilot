"""Tool abstraction: one interface, native and MCP implementations."""

from .base import EmptyArgs, Tool, ToolContext, ToolResult, ToolSpec
from .native import build_native_tools
from .policy import PolicyDecision, ToolPolicy
from .registry import RegisteredTool, ToolRegistry

__all__ = [
    "EmptyArgs",
    "PolicyDecision",
    "RegisteredTool",
    "Tool",
    "ToolContext",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "build_native_tools",
]
