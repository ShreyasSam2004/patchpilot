"""Sandbox protocol.

Everything the agent executes — tests, git, arbitrary commands — goes through a
:class:`Sandbox`. Implementations differ in *how much* isolation they provide,
never in their interface, so the agent and tools are written once.
"""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# Environment variables a child process is allowed to inherit. Everything else
# — API keys, tokens, SSH agent sockets, cloud credentials — is dropped.
ENV_ALLOW_LIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "LOCALAPPDATA",
        "APPDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PYTHONHASHSEED",
        "VIRTUAL_ENV",
    }
)

# Substrings that mark a variable as sensitive even if it reaches us some other
# way. Checked case-insensitively as a defence in depth over the allow-list.
SECRET_MARKERS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "SESSION",
    "COOKIE",
    "AUTH",
)


@dataclass(slots=True)
class ExecResult:
    """Outcome of one command execution."""

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    cwd: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def command(self) -> str:
        return shlex.join(self.argv)

    @property
    def combined_output(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout}\n--- stderr ---\n{self.stderr}"
        return self.stdout or self.stderr

    def summary(self, limit: int = 400) -> str:
        head = f"exit={self.exit_code} in {self.duration_ms}ms"
        if self.timed_out:
            head += " (TIMED OUT)"
        body = self.combined_output.strip()
        if len(body) > limit:
            body = body[:limit] + " ...[truncated]"
        return f"{head}\n{body}" if body else head


@dataclass(slots=True)
class SandboxLimits:
    """Resource ceilings applied to every command."""

    timeout_s: int = 300
    max_output_bytes: int = 256_000
    memory: str = "2g"
    cpus: str = "2"
    pids_limit: int = 512
    network: str = "none"


@dataclass(slots=True)
class SandboxInfo:
    """What the running sandbox actually provides — reported honestly to the UI."""

    kind: str
    available: bool
    isolation: str
    detail: str = ""
    warnings: list[str] = field(default_factory=list)


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a scrubbed environment from the current process environment."""
    import os

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper not in ENV_ALLOW_LIST:
            continue
        if any(marker in upper for marker in SECRET_MARKERS):
            continue
        env[key] = value
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("NO_COLOR", "1")
    if extra:
        for key, value in extra.items():
            if any(marker in key.upper() for marker in SECRET_MARKERS):
                continue
            env[key] = value
    return env


class Sandbox(ABC):
    """Executes commands under resource limits, rooted at a workspace path."""

    kind: str = "abstract"

    def __init__(self, root: Path, limits: SandboxLimits | None = None) -> None:
        self.root = root.resolve()
        self.limits = limits or SandboxLimits()
        self.env_overlay: dict[str, str] = {}
        """Environment applied to every command in this sandbox.

        Set once, after probing, by :mod:`patchpilot.agent.environment`. Its
        purpose is to put the project's real interpreter on ``PATH`` so the
        agent does not have to rediscover it: in a SWE-bench image the working
        Python lives in a conda env the image never activates, and bare
        ``python`` is missing the project's dependencies entirely.

        A caller's explicit ``env=`` still wins, so a tool can override it.
        """

    @abstractmethod
    async def exec(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        """Run ``argv`` (never a shell string) inside the sandbox."""

    async def start(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Acquire sandbox resources. A no-op for sandboxes that need none."""

    async def close(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release sandbox resources. A no-op for sandboxes that hold none."""

    @abstractmethod
    def info(self) -> SandboxInfo:
        """Describe the isolation actually in force."""

    @property
    def path_separator(self) -> str:
        """Separator for PATH entries *inside* the sandbox.

        Not the host's: a Docker sandbox is Linux however it was launched, so
        deriving this from ``os.pathsep`` would corrupt PATH when the API runs
        on Windows.
        """
        return ":"

    def resolve_cwd(self, cwd: str | None) -> Path:
        """Resolve a working directory, refusing anything outside the root."""
        from ..errors import PathEscapeError

        if not cwd:
            return self.root
        candidate = Path(cwd)
        target = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if target != self.root and self.root not in target.parents:
            raise PathEscapeError(cwd)
        return target

    async def __aenter__(self) -> Sandbox:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
