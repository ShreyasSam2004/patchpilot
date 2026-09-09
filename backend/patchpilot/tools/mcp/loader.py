"""MCP configuration loading and registration.

Reads ``mcp.config.json`` (Claude Desktop / VS Code shaped ``mcpServers`` map),
connects to each enabled server, and registers its tools into the registry —
where the policy immediately decides which of them the agent may actually call.

A server that fails to start is logged and skipped: an unavailable MCP server
must degrade the tool set, never break the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...config import Settings
from ...logging_setup import get_logger
from ..registry import ToolRegistry
from .client import McpServerConfig, McpSession

log = get_logger(__name__)


def load_config(path: Path) -> list[McpServerConfig]:
    """Parse an MCP configuration file. Missing file means no servers."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("mcp.config_unreadable", path=str(path), error=str(exc))
        return []

    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        log.warning("mcp.config_malformed", path=str(path))
        return []
    return [
        McpServerConfig.from_dict(name, spec)
        for name, spec in servers.items()
        if isinstance(spec, dict)
    ]


async def load_mcp_tools(settings: Settings, registry: ToolRegistry) -> list[McpSession]:
    """Connect to configured servers and register their tools.

    Returns the open sessions so the caller can close them when the run ends.
    """
    configs = [c for c in load_config(settings.mcp_config_path) if c.enabled]
    if not configs:
        log.info("mcp.no_servers", path=str(settings.mcp_config_path))
        return []

    sessions: list[McpSession] = []
    for config in configs:
        session = McpSession(config)
        try:
            tools = await session.connect()
        except Exception as exc:
            log.warning("mcp.server_failed", server=config.name, error=str(exc))
            await session.close()
            continue

        sessions.append(session)
        for tool in tools:
            registered_as = registry.register(tool, server=config.name)
            decision = registry.policy.decide(registered_as)
            log.info(
                "mcp.tool_registered",
                server=config.name,
                tool=tool.name,
                registered_as=registered_as,
                allowed=decision.allowed,
                reason=decision.reason,
            )
    return sessions


async def mcp_status(settings: Settings) -> dict[str, Any]:
    """Describe MCP configuration for the settings UI (without connecting)."""
    configs = load_config(settings.mcp_config_path)
    import importlib.util

    try:
        installed = importlib.util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        installed = False

    return {
        "enabled": settings.mcp_enabled,
        "package_installed": installed,
        "config_path": str(settings.mcp_config_path),
        "config_exists": settings.mcp_config_path.exists(),
        "servers": [c.describe() for c in configs],
        "note": (
            "The MCP server decides which tools exist; PatchPilot's policy decides "
            "which of them this agent may call."
        ),
    }


async def probe_servers(settings: Settings) -> list[dict[str, Any]]:
    """Connect to each server and report the tools it offers, plus the verdict."""
    from ..policy import ToolPolicy

    policy = ToolPolicy.from_settings(settings)
    results: list[dict[str, Any]] = []

    for config in load_config(settings.mcp_config_path):
        if not config.enabled:
            results.append({**config.describe(), "status": "disabled", "tools": []})
            continue
        session = McpSession(config)
        try:
            tools = await session.connect()
            results.append(
                {
                    **config.describe(),
                    "status": "connected",
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description[:300],
                            "allowed": policy.permits(tool.name),
                            "reason": policy.decide(tool.name).reason,
                        }
                        for tool in tools
                    ],
                }
            )
        except Exception as exc:
            results.append({**config.describe(), "status": "error", "error": str(exc), "tools": []})
        finally:
            await session.close()
    return results
