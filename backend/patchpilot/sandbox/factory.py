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
from .docker import DockerSandbox, docker_available, image_present
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
    image: str | None = None,
    pull_if_missing: bool = False,
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
                root,
                limits,
                image=image or settings.docker_image,
                container_name=name,
                pull_if_missing=pull_if_missing,
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
    has_image = await image_present(settings.docker_image) if docker_ok else False
    usable = docker_ok and has_image
    return {
        "configured": settings.sandbox,
        "docker_available": docker_ok,
        "docker_detail": docker_detail,
        "docker_image": settings.docker_image,
        "docker_image_present": has_image,
        "effective": (
            settings.sandbox if (settings.sandbox != "docker" or usable) else "unavailable"
        ),
    }
