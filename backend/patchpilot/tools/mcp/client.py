"""MCP client.

Connects to configured MCP servers over stdio, discovers their tools, and wraps
each one in the same :class:`~patchpilot.tools.base.Tool` interface the native
tools implement. The agent cannot tell the difference — which is the point:
tool *origin* is an infrastructure concern, and tool *permission* is ours.

Security posture:

* the MCP server decides what tools exist;
* PatchPilot's :class:`~patchpilot.tools.policy.ToolPolicy` decides which of
  them this agent may call — discovery never implies permission;
* a discovered tool that collides with a native name is registered under a
  qualified ``mcp__<server>__<tool>`` name so it can never shadow a trusted
  local implementation;
* server-supplied descriptions are passed to the model but are treated as
  untrusted text and truncated.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, create_model

from ...domain.enums import ToolOrigin
from ...errors import ToolError
from ...logging_setup import get_logger
from ..base import Tool, ToolContext, ToolResult

log = get_logger(__name__)

MAX_DESCRIPTION_CHARS = 2000
MCP_CALL_TIMEOUT_S = 120.0
MCP_CONNECT_TIMEOUT_S = 30.0


@dataclass
class McpServerConfig:
    """One configured MCP server."""

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    transport: str = "stdio"

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> McpServerConfig:
        return cls(
            name=name,
            command=data.get("command", ""),
            args=list(data.get("args", [])),
            env=dict(data.get("env", {})),
            url=data.get("url"),
            enabled=bool(data.get("enabled", True)),
            transport=data.get("transport", "http" if data.get("url") else "stdio"),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "url": self.url,
            "enabled": self.enabled,
            # Never echo env values — they are how a server receives secrets.
            "env_keys": sorted(self.env),
        }


def _schema_to_model(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Build a Pydantic model from an MCP tool's JSON schema.

    Only the top level is mapped to typed fields; nested objects stay ``dict``.
    That is enough for validation and for producing a schema the model can use,
    without trying to be a full JSON-Schema compiler.
    """
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])

    type_map: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        json_type = spec.get("type")
        if isinstance(json_type, list):
            json_type = next((t for t in json_type if t != "null"), "string")
        python_type = type_map.get(json_type or "string", Any)
        if field_name in required:
            fields[field_name] = (python_type, ...)
        else:
            fields[field_name] = (python_type | None, spec.get("default"))

    if not fields:
        return create_model(f"{name}Args")  # type: ignore[return-value]
    return create_model(f"{name}Args", **fields)  # type: ignore[return-value,call-overload]


class McpTool(Tool):
    """A tool discovered on an MCP server, exposed through the native interface."""

    origin = ToolOrigin.MCP

    def __init__(
        self,
        *,
        session: Any,
        server: str,
        name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> None:
        self._session = session
        self.server = server
        self.remote_name = name
        self.name = name
        self.description = (
            f"[via MCP server '{server}'] {description or 'no description provided'}"
        )[:MAX_DESCRIPTION_CHARS]
        self.args_model = _schema_to_model(name, input_schema)
        self._raw_schema = input_schema or {"type": "object", "properties": {}}
        self.timeout_s = MCP_CALL_TIMEOUT_S
        # Conservative: assume an unknown remote tool can mutate the workspace,
        # so the anti-loop logic treats a repeat as a real repeat.
        self.mutating = True

    def json_schema(self) -> dict[str, Any]:
        """Prefer the server's own schema; it is authoritative for its tool."""
        schema = dict(self._raw_schema)
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.pop("title", None)
        return schema

    def parse_args(self, raw: dict[str, Any]) -> BaseModel:
        # Validate leniently: the server performs its own validation, and being
        # stricter than the server would reject calls it would have accepted.
        try:
            return self.args_model.model_validate(raw or {})
        except Exception:
            return _Passthrough(arguments=raw or {})

    async def execute(self, ctx: ToolContext, args: Any) -> ToolResult:
        arguments = (
            args.arguments
            if isinstance(args, _Passthrough)
            else args.model_dump(exclude_none=True)
        )
        # `Tool.run` does not enforce `timeout_s`: native tools are bounded by
        # their sandbox. An MCP call is bounded by nothing else, and one that
        # never answers would hold the run forever while its heartbeat kept it
        # looking alive.
        try:
            async with asyncio.timeout(self.timeout_s):
                response = await self._session.call_tool(self.remote_name, arguments)
        except TimeoutError as exc:
            raise ToolError(
                f"MCP server {self.server!r} did not answer {self.remote_name!r} "
                f"within {self.timeout_s:.0f}s",
                code="mcp_timeout",
            ) from exc
        except Exception as exc:
            raise ToolError(
                f"MCP server {self.server!r} failed to run {self.remote_name!r}: {exc}",
                code="mcp_error",
            ) from exc

        text = _render_content(response)
        is_error = bool(getattr(response, "isError", False))
        budget = ctx.settings.max_tool_output_bytes
        truncated = len(text) > budget
        if truncated:
            text = text[:budget] + "\n...[MCP output truncated]"

        if is_error:
            return ToolResult.failure(text or "MCP tool reported an error", code="mcp_tool_error")
        return ToolResult(
            ok=True,
            content=text or "(no output)",
            summary=f"{self.remote_name} via {self.server}",
            truncated=truncated,
            data={"server": self.server, "tool": self.remote_name},
        )


class _Passthrough(BaseModel):
    arguments: dict[str, Any] = {}


def _render_content(response: Any) -> str:
    """Flatten an MCP tool result into text for the model."""
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", ""))
        elif block_type == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            parts.append(text if text else f"[resource {getattr(resource, 'uri', '?')}]")
        elif block_type == "image":
            parts.append("[image content omitted]")
        else:
            parts.append(str(block))
    structured = getattr(response, "structuredContent", None)
    if not parts and structured:
        parts.append(str(structured))
    return "\n".join(p for p in parts if p).strip()


class McpSession:
    """An open connection to one MCP server."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self._session: Any = None
        self._stack: Any = None
        self.tools: list[McpTool] = []

    async def connect(self) -> list[McpTool]:
        """Open the connection and discover tools."""
        from contextlib import AsyncExitStack

        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise ToolError(
                "MCP support requires the 'mcp' package: pip install -e '.[mcp]'",
                code="mcp_missing",
            ) from exc

        if self.config.transport != "stdio":
            raise ToolError(
                f"MCP transport {self.config.transport!r} is not supported yet; use stdio",
                code="mcp_transport",
            )
        if not self.config.command:
            raise ToolError(
                f"MCP server {self.config.name!r} has no command configured", code="mcp_config"
            )
        if shutil.which(self.config.command) is None:
            raise ToolError(
                f"MCP server {self.config.name!r} command not found on PATH: "
                f"{self.config.command}",
                code="mcp_config",
            )

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=self.config.env or None,
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        # A server that starts but never answers must not hang run start-up or
        # the status page; the caller closes the session and skips the server.
        async with asyncio.timeout(MCP_CONNECT_TIMEOUT_S):
            await self._session.initialize()
            listed = await self._session.list_tools()
        self.tools = [
            McpTool(
                session=self._session,
                server=self.config.name,
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema or {}),
            )
            for tool in listed.tools
        ]
        log.info(
            "mcp.connected",
            server=self.config.name,
            tools=[t.name for t in self.tools],
        )
        return self.tools

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except Exception as exc:
                log.warning("mcp.close_failed", server=self.config.name, error=str(exc))
            self._stack = None
            self._session = None
