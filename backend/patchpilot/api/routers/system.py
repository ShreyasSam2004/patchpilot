"""System status, configuration and health endpoints.

The status payload is deliberately blunt about what is and is not available:
whether a model key is present, whether Docker is running, whether the SWE-bench
harness is installed. The UI surfaces these as warnings rather than letting a
user start a run that cannot possibly work.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ... import __version__
from ...agent.session import build_registry
from ...bench.dataset import dataset_available
from ...bench.evaluator import evaluator_preflight
from ...config import Settings, get_settings
from ...db.session import get_session
from ...providers.factory import provider_status
from ...runs import service as run_service
from ...sandbox.factory import preflight as sandbox_preflight
from ...tools.mcp.loader import mcp_status
from ...tools.policy import ToolPolicy
from ...worker.queue import queue_depth
from ..schemas import SystemStatusOut

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/status", response_model=SystemStatusOut)
async def status(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SystemStatusOut:
    provider = provider_status(settings)
    sandbox = await sandbox_preflight(settings)
    swebench = await evaluator_preflight(settings)
    dataset_ok, dataset_detail = dataset_available()
    swebench["dataset_available"] = dataset_ok
    swebench["dataset_detail"] = dataset_detail
    swebench["dataset"] = settings.swebench_dataset
    swebench["split"] = settings.swebench_split

    registry, sessions = await build_registry(settings)
    for mcp_session in sessions:
        await mcp_session.close()

    warnings: list[str] = []
    if not provider["ready"]:
        warnings.append(str(provider["detail"]))
    if provider.get("is_test_double"):
        warnings.append(
            "The scripted test-double provider is selected. Runs will not use a real model."
        )
    if settings.sandbox == "local":
        warnings.append(
            "Local sandbox in use: repository code runs as your user account without "
            "kernel isolation. Set PATCHPILOT_SANDBOX=docker for untrusted repositories."
        )
    if settings.sandbox == "docker" and not sandbox["docker_available"]:
        warnings.append(f"Docker sandbox configured but unavailable: {sandbox['docker_detail']}")
    if not swebench["ready"]:
        warnings.extend(swebench.get("problems", []))

    return SystemStatusOut(
        version=__version__,
        environment=settings.environment,
        provider=provider,
        sandbox=sandbox,
        swebench=swebench,
        tools=registry.inventory(),
        policy=ToolPolicy.from_settings(settings).describe(),
        mcp=await mcp_status(settings),
        budgets={
            "max_iterations": settings.max_iterations,
            "max_tool_calls": settings.max_tool_calls,
            "max_runtime_s": settings.max_runtime_s,
            "max_test_runtime_s": settings.max_test_runtime_s,
            "max_cost_usd": settings.max_cost_usd,
        },
        database="sqlite" if settings.is_sqlite else "postgresql",
        queue_depth=await queue_depth(session),
        warnings=warnings,
    )


@router.get("/config")
async def configuration(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    """The effective configuration, with secrets reduced to 'set' / null."""
    return settings.redacted()


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await run_service.summary_stats(session)
