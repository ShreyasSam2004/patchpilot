"""Docker sandbox.

One long-lived container per run. The workspace is bind-mounted at
``/workspace``; nothing else from the host is visible. The container is started
with:

* ``--network none``      no egress, no lateral movement
* ``--memory`` / ``--cpus`` / ``--pids-limit``   resource ceilings
* ``--cap-drop ALL`` and ``--security-opt no-new-privileges``
* ``--env`` populated only from the sandbox allow-list

Commands are delivered with ``docker exec`` as an argv vector, so there is no
shell interpolation on either side of the boundary.

This is the correct posture for running untrusted repository code and is what
the SWE-bench path expects. If the Docker daemon is unreachable, construction
raises :class:`SandboxError` rather than silently degrading to local execution —
the caller decides whether to fall back, and the UI is told which sandbox is
actually in force.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from ..errors import SandboxError
from ..logging_setup import get_logger
from .base import ExecResult, Sandbox, SandboxInfo, SandboxLimits, build_env

log = get_logger(__name__)

CONTAINER_WORKDIR = "/workspace"


async def _run_host(argv: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Run a docker CLI command on the host (not inside the sandbox)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return 124, "", f"docker command timed out after {timeout}s"
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def docker_available() -> tuple[bool, str]:
    """Check that the Docker CLI exists *and* the daemon answers."""
    if shutil.which("docker") is None:
        return False, "docker CLI not found on PATH"
    code, out, err = await _run_host(["docker", "info", "--format", "{{json .}}"], timeout=30)
    if code != 0:
        detail = (err or out).strip()
        return False, detail.splitlines()[-1] if detail else "daemon unreachable"
    try:
        info = json.loads(out)
        return True, f"{info.get('ServerVersion', '?')} ({info.get('OSType', '?')})"
    except json.JSONDecodeError:
        return True, "daemon reachable"


class DockerSandbox(Sandbox):
    """Container-isolated execution."""

    kind = "docker"

    def __init__(
        self,
        root: Path,
        limits: SandboxLimits | None = None,
        *,
        image: str = "python:3.11-slim",
        container_name: str | None = None,
    ) -> None:
        super().__init__(root, limits)
        self.image = image
        self.container_name = container_name or f"patchpilot-{root.name}"
        self._container_id: str | None = None
        self._daemon_detail = ""

    async def start(self) -> None:
        available, detail = await docker_available()
        self._daemon_detail = detail
        if not available:
            raise SandboxError(
                f"Docker sandbox requested but the daemon is unavailable: {detail}. "
                "Start Docker, or set PATCHPILOT_SANDBOX=local (weaker isolation)."
            )

        await self._remove_existing()

        argv = [
            "docker", "run", "--detach",
            "--name", self.container_name,
            "--workdir", CONTAINER_WORKDIR,
            "--volume", f"{self.root}:{CONTAINER_WORKDIR}",
            "--network", self.limits.network,
            "--memory", self.limits.memory,
            "--memory-swap", self.limits.memory,
            "--cpus", self.limits.cpus,
            "--pids-limit", str(self.limits.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
        ]
        for key, value in build_env().items():
            if key.upper() in {"PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR",
                               "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
                               "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "VIRTUAL_ENV",
                               "HOME", "USER", "USERNAME", "SHELL"}:
                continue  # host-specific values are meaningless in the container
            argv += ["--env", f"{key}={value}"]
        argv += [self.image, "sleep", "infinity"]

        code, out, err = await _run_host(argv, timeout=300)
        if code != 0:
            raise SandboxError(f"failed to start container: {(err or out).strip()}")
        self._container_id = out.strip()
        log.info("sandbox.docker.started", container=self.container_name, image=self.image)

    async def _remove_existing(self) -> None:
        await _run_host(["docker", "rm", "-f", self.container_name], timeout=60)

    async def close(self) -> None:
        if self._container_id is None:
            return
        await _run_host(["docker", "rm", "-f", self.container_name], timeout=60)
        log.info("sandbox.docker.removed", container=self.container_name)
        self._container_id = None

    def _container_path(self, cwd: str | None) -> str:
        target = self.resolve_cwd(cwd)
        relative = target.relative_to(self.root)
        if relative == Path("."):
            return CONTAINER_WORKDIR
        return f"{CONTAINER_WORKDIR}/{relative.as_posix()}"

    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if self._container_id is None:
            raise SandboxError("docker sandbox used before start()")
        if not argv:
            raise ValueError("argv must not be empty")

        timeout = timeout_s or self.limits.timeout_s
        workdir = self._container_path(cwd)

        docker_argv = ["docker", "exec", "--workdir", workdir]
        for key, value in (env or {}).items():
            docker_argv += ["--env", f"{key}={value}"]
        docker_argv += [self.container_name, *argv]

        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *docker_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        timed_out = False
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            proc.kill()
            # `docker exec` dying does not kill the in-container process; do it explicitly.
            await _run_host(
                ["docker", "exec", self.container_name, "pkill", "-9", "-f", argv[0]],
                timeout=30,
            )
            out_b, err_b = b"", b""

        cap = self.limits.max_output_bytes
        stdout = out_b[:cap].decode("utf-8", "replace")
        stderr = err_b[:cap].decode("utf-8", "replace")
        truncated = len(out_b) > cap or len(err_b) > cap
        if timed_out:
            stderr = (stderr + f"\n[patchpilot] command exceeded {timeout}s and was killed").strip()

        return ExecResult(
            argv=argv,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.perf_counter() - started) * 1000),
            timed_out=timed_out,
            truncated=truncated,
            cwd=workdir,
        )

    def info(self) -> SandboxInfo:
        return SandboxInfo(
            kind="docker",
            available=self._container_id is not None,
            isolation="container",
            detail=(
                f"image={self.image} network={self.limits.network} "
                f"memory={self.limits.memory} cpus={self.limits.cpus} "
                f"pids={self.limits.pids_limit} caps=dropped daemon={self._daemon_detail}"
            ),
            warnings=[],
        )
