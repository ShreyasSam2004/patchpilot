"""MCP inspection endpoints.

These make the permission model visible: what a server offers, and what
PatchPilot will actually let the agent call.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...agent.session import build_registry
from ...config import Settings, get_settings
from ...tools.mcp.loader import mcp_status, probe_servers
from ...tools.policy import ToolPolicy

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("/status")
async def status(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return await mcp_status(settings)


@router.get("/probe")
async def probe(settings: Settings = Depends(get_settings)) -> list[dict[str, Any]]:
    """Connect to each configured server and list its tools with the policy verdict."""
    return await probe_servers(settings)


@router.get("/tools")
async def tools(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """Full tool inventory the agent would see, native and MCP together."""
    registry, sessions = await build_registry(settings)
    try:
        inventory = registry.inventory()
    finally:
        for session in sessions:
            await session.close()
    return {
        "policy": ToolPolicy.from_settings(settings).describe(),
        "tools": inventory,
        "allowed_count": sum(1 for t in inventory if t["allowed"]),
        "blocked_count": sum(1 for t in inventory if not t["allowed"]),
    }
