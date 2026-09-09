"""Local subprocess sandbox.

Isolation provided:

* commands run as an argv list — **never** through a shell;
* the working directory is jailed to the workspace root;
* the environment is rebuilt from an allow-list, so no API key, token or SSH
  agent socket is inherited;
* wall-clock timeout with a process-*tree* kill (a plain ``proc.kill()`` leaves
  pytest's children running);
* stdout/stderr are read with a hard byte cap so a runaway test cannot exhaust
  memory.

It does **not** provide kernel-level isolation. For untrusted repositories use
:class:`~patchpilot.sandbox.docker.DockerSandbox`; ``info()`` says so plainly so
the UI can warn rather than imply safety it does not have.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..logging_setup import get_logger
from .base import ExecResult, Sandbox, SandboxInfo, SandboxLimits, build_env

log = get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"


async def _read_capped(stream: asyncio.StreamReader | None, limit: int) -> tuple[str, bool]:
    """Read a stream up to ``limit`` bytes, draining (and discarding) the rest."""
    if stream is None:
        return "", False
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            break
        if total < limit:
            take = chunk[: max(0, limit - total)]
            chunks.append(take)
            total += len(take)
            if len(take) < len(chunk):
                truncated = True
        else:
            truncated = True
    return b"".join(chunks).decode("utf-8", errors="replace"), truncated


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process and every child it spawned."""
    if proc.returncode is not None:
        return
    try:
        # `sys.platform` (not a module-level constant) so type checkers can
        # narrow away the POSIX-only calls on Windows and vice versa.
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


class LocalSandbox(Sandbox):
    """Subprocess execution with a cwd jail, scrubbed env, timeouts and caps."""

    kind = "local"

    def __init__(self, root: Path, limits: SandboxLimits | None = None) -> None:
        super().__init__(root, limits)
        self.root.mkdir(parents=True, exist_ok=True)

    def normalize_argv(self, argv: list[str]) -> list[str]:
        """Point a bare interpreter name at the interpreter running PatchPilot.

        ``python -m pytest`` is the most common test command there is, but a
        bare ``python`` resolves against ``PATH``, which is frequently a
        *different* interpreter from the one PatchPilot runs under — most
        obviously when PatchPilot is installed in a virtualenv, because running
        ``.venv/bin/python`` does not put ``.venv/bin`` on ``PATH``. The agent
        then sees "No module named pytest" and cannot validate anything.

        Since the local sandbox by definition executes in this host
        environment, mapping the bare name onto ``sys.executable`` is both the
        least surprising behaviour and the only one that reliably works. An
        explicit path supplied by the caller is never rewritten, and the Docker
        sandbox does not do this at all — the container has its own Python.
        """
        if not argv:
            return argv
        first = argv[0]
        base = first.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")

        if first == base and base in {"python", "python3", "py"}:
            return [sys.executable, *argv[1:]]

        # A bare `pytest` that is not on PATH is almost always installed as a
        # module in the current environment.
        if first == base and base == "pytest" and shutil.which("pytest") is None:
            return [sys.executable, "-m", "pytest", *argv[1:]]

        return argv

    @property
    def path_separator(self) -> str:
        return os.pathsep

    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        if not argv:
            raise ValueError("argv must not be empty")

        argv = self.normalize_argv(argv)
        workdir = self.resolve_cwd(cwd)
        workdir.mkdir(parents=True, exist_ok=True)
        timeout = timeout_s or self.limits.timeout_s
        process_env = build_env({**self.env_overlay, **(env or {})})
        process_env["PWD"] = str(workdir)

        kwargs: dict[str, object] = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # own process group -> killpg works

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workdir),
                env=process_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                **kwargs,  # type: ignore[arg-type]
            )
        except FileNotFoundError:
            duration = int((time.perf_counter() - started) * 1000)
            return ExecResult(
                argv=argv,
                exit_code=127,
                stdout="",
                stderr=f"executable not found: {argv[0]}",
                duration_ms=duration,
                cwd=str(workdir),
            )
        except OSError as exc:
            duration = int((time.perf_counter() - started) * 1000)
            return ExecResult(
                argv=argv,
                exit_code=126,
                stdout="",
                stderr=f"failed to launch: {exc}",
                duration_ms=duration,
                cwd=str(workdir),
            )

        cap = self.limits.max_output_bytes
        reader = asyncio.gather(
            _read_capped(proc.stdout, cap),
            _read_capped(proc.stderr, cap),
        )
        timed_out = False
        try:
            (stdout, out_trunc), (stderr, err_trunc) = await asyncio.wait_for(
                reader, timeout=timeout
            )
            await proc.wait()
        except TimeoutError:
            timed_out = True
            _kill_tree(proc)
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            stdout, stderr, out_trunc, err_trunc = "", "", False, False
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)

        duration = int((time.perf_counter() - started) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1
        if timed_out:
            stderr = (stderr + f"\n[patchpilot] command exceeded {timeout}s and was killed").strip()

        result = ExecResult(
            argv=argv,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=timed_out,
            truncated=out_trunc or err_trunc,
            cwd=str(workdir),
        )
        log.debug(
            "sandbox.exec",
            sandbox=self.kind,
            argv=argv[:4],
            exit_code=result.exit_code,
            ms=duration,
            timed_out=timed_out,
        )
        return result

    def info(self) -> SandboxInfo:
        return SandboxInfo(
            kind="local",
            available=True,
            isolation="process",
            detail=(
                "Subprocess with a working-directory jail, an environment allow-list, "
                "wall-clock timeouts, process-tree kill and output byte caps."
            ),
            warnings=[
                "No kernel-level isolation: repository code runs as your user account.",
                "Use PATCHPILOT_SANDBOX=docker for untrusted repositories.",
            ],
        )
