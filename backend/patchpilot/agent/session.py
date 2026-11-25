"""Run assembly.

Wires a task into a workspace, sandbox, tool registry and provider, runs the
orchestrator, and tears everything down. This is the single entry point used by
the CLI, the background worker and the benchmark runner, so all three exercise
exactly the same code path.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..domain.enums import RunState
from ..errors import WorkspaceError
from ..events.bus import EventBus, EventSink, LoggingSink
from ..fsutil import remove_tree
from ..logging_setup import get_logger
from ..providers.base import ModelProvider
from ..providers.factory import create_provider
from ..repair.progress import refuse_test_writes
from ..sandbox.base import Sandbox
from ..sandbox.factory import create_sandbox, limits_from_settings
from ..sandbox.local import LocalSandbox
from ..tools.base import Tool
from ..tools.native import build_native_tools
from ..tools.policy import ToolPolicy
from ..tools.registry import ToolRegistry
from ..workspace.workspace import Workspace, prepare_workspace
from .orchestrator import AgentOrchestrator, AgentTask
from .result import AgentResult

log = get_logger(__name__)


@dataclass
class SessionConfig:
    """Per-run overrides on top of application settings."""

    provider: str | None = None
    model: str | None = None
    sandbox: str | None = None
    docker_image: str | None = None
    """Override the sandbox image for this run. Benchmark runs point this at the
    official per-instance SWE-bench image so the agent can actually run the
    repository's tests."""
    pull_image: bool = False
    allow_sandbox_fallback: bool = False
    """Degrade a requested docker sandbox to local execution if Docker is down.

    Off by default, matching the sandbox factory: a run configured for
    container isolation must not quietly execute repository code on the host.
    Without Docker the run fails with the reason and the fix, and an operator
    who wants local execution asks for it explicitly."""
    keep_workspace: bool = False
    event_seq_start: int = 0
    """Last event number this run id has already recorded, so a re-execution
    (a revision, or recovery from a crashed worker) continues the sequence."""
    policy: ToolPolicy | None = None
    extra_tools: Sequence[Tool] = ()
    mcp_enabled: bool | None = None

    def describe(self, settings: Settings) -> dict[str, Any]:
        """The configuration actually used — persisted for reproducibility."""
        return {
            "provider": self.provider or settings.provider,
            "model": self.model or settings.model,
            "sandbox": self.sandbox or settings.sandbox,
            "docker_image": self.docker_image or settings.docker_image,
            "max_iterations": settings.max_iterations,
            "max_tool_calls": settings.max_tool_calls,
            "max_runtime_s": settings.max_runtime_s,
            "max_test_runtime_s": settings.max_test_runtime_s,
            "max_cost_usd": settings.max_cost_usd,
            "mcp_enabled": settings.mcp_enabled if self.mcp_enabled is None else self.mcp_enabled,
            "tool_policy": (self.policy or ToolPolicy.from_settings(settings)).describe(),
        }


async def build_registry(
    settings: Settings, config: SessionConfig | None = None
) -> tuple[ToolRegistry, list[Any]]:
    """Assemble the tool registry: native tools plus any MCP tools.

    Returns the registry and the list of MCP sessions to close afterwards.
    """
    config = config or SessionConfig()
    policy = config.policy or ToolPolicy.from_settings(settings)
    registry = ToolRegistry(policy=policy)
    registry.register_all(build_native_tools())
    for tool in config.extra_tools:
        registry.register(tool)

    mcp_enabled = settings.mcp_enabled if config.mcp_enabled is None else config.mcp_enabled
    sessions: list[Any] = []
    if mcp_enabled:
        from ..tools.mcp.loader import load_mcp_tools

        sessions = await load_mcp_tools(settings, registry)

    return registry, sessions


@asynccontextmanager
async def agent_session(
    task: AgentTask,
    *,
    settings: Settings | None = None,
    config: SessionConfig | None = None,
    provider: ModelProvider | None = None,
    sinks: Sequence[EventSink] = (),
):
    """Prepare everything a run needs, yield the orchestrator, then clean up."""
    settings = settings or get_settings()
    config = config or SessionConfig()
    settings.ensure_dirs()

    workspace_root = settings.workspace_root / task.run_id
    sandbox: Sandbox | None = None
    provisioner: Sandbox | None = None
    owns_provider = provider is None
    mcp_sessions: list[Any] = []

    bus = EventBus(task.run_id, [LoggingSink(), *sinks], start_seq=config.event_seq_start)

    try:
        sandbox = await create_sandbox(
            workspace_root,
            settings,
            kind=config.sandbox,
            allow_fallback=config.allow_sandbox_fallback,
            name=f"patchpilot-{task.run_id[:12]}",
            image=config.docker_image,
            pull_if_missing=config.pull_image,
        )
        # Clone/fetch need network; the Docker sandbox runs with --network
        # none. Provisioning is PatchPilot's own trusted operation, so it uses
        # a host executor, while everything the agent does stays in `sandbox`.
        provisioner = LocalSandbox(workspace_root, limits_from_settings(settings))
        await provisioner.start()
        workspace = await _prepare(task, workspace_root, sandbox, provisioner)

        registry, mcp_sessions = await build_registry(settings, config)
        if provider is None:
            provider = create_provider(settings, provider=config.provider, model=config.model)

        orchestrator = AgentOrchestrator(
            task=task,
            workspace=workspace,
            sandbox=sandbox,
            registry=registry,
            provider=provider,
            settings=settings,
            bus=bus,
        )
        yield orchestrator
    finally:
        for session in mcp_sessions:
            try:
                await session.close()
            except Exception as exc:
                log.warning("session.mcp_close_failed", error=str(exc))
        if owns_provider and provider is not None:
            try:
                await provider.close()
            except Exception as exc:
                log.warning("session.provider_close_failed", error=str(exc))
        if sandbox is not None:
            try:
                await sandbox.close()
            except Exception as exc:
                log.warning("session.sandbox_close_failed", error=str(exc))
        if not config.keep_workspace:
            remove_tree(workspace_root)


async def _prepare(
    task: AgentTask, root: Path, sandbox: Sandbox, provisioner: Sandbox | None = None
) -> Workspace:
    if task.local_path is None and not task.repo_url:
        raise WorkspaceError("a run needs either a repository URL or a local path")
    return await prepare_workspace(
        root,
        sandbox,
        repo_url=task.repo_url,
        local_path=task.local_path,
        base_commit=task.base_commit,
        provisioner=provisioner,
        write_guard=refuse_test_writes if task.protect_tests else None,
    )


async def run_agent_task(
    task: AgentTask,
    *,
    settings: Settings | None = None,
    config: SessionConfig | None = None,
    provider: ModelProvider | None = None,
    sinks: Sequence[EventSink] = (),
) -> AgentResult:
    """Run one task end to end and return its result."""
    settings = settings or get_settings()
    try:
        async with agent_session(
            task, settings=settings, config=config, provider=provider, sinks=sinks
        ) as orchestrator:
            return await orchestrator.run()
    except Exception as exc:
        log.exception("session.failed", run_id=task.run_id)
        result = AgentResult(run_id=task.run_id, state=RunState.FAILED)
        result.error = f"{type(exc).__name__}: {exc}"
        result.stop_reason = f"run could not start: {exc}"
        result.summary = result.stop_reason
        return result
