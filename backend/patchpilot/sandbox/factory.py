"""Sandbox selection.

The caller asks for a kind; the factory returns a started sandbox or explains
why it could not. It never silently substitutes weaker isolation — the run
record stores which sandbox was actually used.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..errors import SandboxError
from ..logging_setup import get_logger
from .base import Sandbox, SandboxLimits
from .docker import DockerSandbox, docker_available
from .local import LocalSandbox

log = get_logger(__name__)


def limits_from_settings(settings: Settings, *, timeout_s: int | None = None) -> SandboxLimits:
    return SandboxLimits(
        timeout_s=timeout_s or settings.command_timeout_s,
        max_output_bytes=max(settings.max_tool_output_bytes * 8, 64_000),
        memory=settings.docker_memory,
        cpus=settings.docker_cpus,
        pids_limit=settings.docker_pids_limit,
        network=settings.docker_network,
    )


async def create_sandbox(
    root: Path,
    settings: Settings,
    *,
    kind: str | None = None,
    allow_fallback: bool = False,
    name: str | None = None,
) -> Sandbox:
    """Create and start a sandbox for ``root``.

    ``allow_fallback`` degrades docker -> local with a loud warning; it is off by
    default so a benchmark run never quietly loses its isolation guarantees.
    """
    requested = kind or settings.sandbox
    limits = limits_from_settings(settings)

    if requested == "docker":
        try:
            sandbox: Sandbox = DockerSandbox(
                root, limits, image=settings.docker_image, container_name=name
            )
            await sandbox.start()
            return sandbox
        except SandboxError:
            if not allow_fallback:
                raise
            log.warning("sandbox.fallback", requested="docker", using="local")
            sandbox = LocalSandbox(root, limits)
            await sandbox.start()
            return sandbox

    sandbox = LocalSandbox(root, limits)
    await sandbox.start()
    return sandbox


async def preflight(settings: Settings) -> dict[str, object]:
    """Report what isolation is genuinely available on this machine."""
    docker_ok, docker_detail = await docker_available()
    return {
        "configured": settings.sandbox,
        "docker_available": docker_ok,
        "docker_detail": docker_detail,
        "effective": (
            settings.sandbox if (settings.sandbox != "docker" or docker_ok) else "unavailable"
        ),
    }
