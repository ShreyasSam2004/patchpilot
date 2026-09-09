"""Discover the project's real Python environment, once, before the agent runs.

Evidence for this module. In the 15-instance SWE-bench Lite batch, 145 of 375
tool calls -- 39% of the entire budget -- went on rediscovering the interpreter.
Every one of the six tasks that produced no patch had burned most of its
iterations this way, and each of the fourteen runs that got there found the same
answer independently::

    it3  run_command  python -c "from sympy import *"                 -> exit 1
    it4  run_command  which python; pip show mpmath                   -> refused
    it6  run_command  python -c "print(sys.executable)"
    it8  run_tests    python -m pytest ...                            -> no output
    it10 run_command  ls /opt/miniconda3/envs
    it11 run_command  /opt/miniconda3/envs/testbed/bin/python -c "import sympy"  -> works

The cause is structural rather than a model weakness. A SWE-bench image keeps
the project's dependencies in a conda environment that the image does not
activate; the official harness activates it inside its own eval script, but an
agent's commands get the bare system interpreter, which has neither the
project's dependencies nor pytest. ``python -m pytest`` there exits 1 with no
output at all, which reads like a broken repository rather than a broken
interpreter -- so the agent goes looking, and the budget is gone.

We therefore probe once at preparation time, put the winning interpreter's
directory first on ``PATH`` for the rest of the run, and tell the agent what was
found. Nothing here is specific to conda, sympy or SWE-bench: it ranks whatever
interpreters the machine offers by whether they can import the project under
test, and degrades to a no-op when it cannot tell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..logging_setup import get_logger
from ..sandbox.base import Sandbox

log = get_logger(__name__)

#: Directories that commonly hold a project's real interpreter. These are
#: expanded inside the sandbox, so an unfamiliar layout still resolves as long
#: as it follows one of the conventions.
CANDIDATE_GLOBS: tuple[str, ...] = (
    "/opt/miniconda3/envs/*/bin/python",
    "/opt/conda/envs/*/bin/python",
    "/usr/share/miniconda/envs/*/bin/python",
    "/root/miniconda3/envs/*/bin/python",
    ".venv/bin/python",
    "venv/bin/python",
    "env/bin/python",
)

#: Interpreters looked up on PATH, tried after the globs above.
CANDIDATE_NAMES: tuple[str, ...] = ("python3", "python")

#: Directory names that are never the project's importable package.
NOT_PACKAGES: frozenset[str] = frozenset(
    {
        "test", "tests", "testing", "doc", "docs", "example", "examples",
        "bin", "scripts", "build", "dist", "data", "benchmarks", "tools",
        "release", "contrib", "vendor",
    }
)


@dataclass(frozen=True)
class RuntimeEnvironment:
    """What the probe established about the sandbox's Python."""

    python: str | None = None
    """Absolute path to the interpreter that can import the project."""
    bin_dir: str | None = None
    """Directory prepended to PATH so bare ``python`` means :attr:`python`."""
    has_pytest: bool = False
    package: str | None = None
    """Top-level package the probe used as its import test, if it found one."""
    imports_project: bool = False
    """True when :attr:`python` actually imported :attr:`package`."""
    probed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.python is not None

    def summary(self) -> dict[str, object]:
        """Structured form, for the event log and run metrics."""
        return {
            "probed": self.probed,
            "python": self.python,
            "bin_dir": self.bin_dir,
            "has_pytest": self.has_pytest,
            "package": self.package,
            "imports_project": self.imports_project,
            "notes": list(self.notes),
        }

    def describe(self) -> str:
        """The briefing the agent sees.

        Written to pre-empt the search it would otherwise run: it names the
        interpreter, says it is already on PATH, and states plainly that
        hunting for one is wasted effort. Without that last part the first
        failing import still sends the agent spelunking.
        """
        if not self.usable:
            return (
                "Environment probe: no working Python interpreter could be confirmed. "
                "Establish the test runner early, and treat import errors as a possible "
                "environment problem rather than a bug in the code."
            )
        lines = [f"The project's interpreter is `{self.python}`."]
        lines.append(
            "It is already first on PATH for every command you run, so plain `python` "
            "and `pytest` resolve to it. Do not spend turns looking for an interpreter, "
            "a virtualenv, conda, or a missing package -- that search is already done."
        )
        if self.imports_project and self.package:
            lines.append(f"It imports `{self.package}` from the working tree.")
        if self.has_pytest:
            lines.append("pytest is installed there, so `python -m pytest -q <path>` works.")
        else:
            lines.append(
                "pytest is NOT installed there. Use the project's own runner instead "
                "(for example `python bin/test <path>` or `python -m unittest`); the "
                "sandbox has no network, so nothing can be installed."
            )
        return " ".join(lines)


def top_level_package(root: Path) -> str | None:
    """Best guess at the project's importable package, from the tree alone."""
    fallback: str | None = None
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        if entry.name.lower() in NOT_PACKAGES:
            continue
        if not (entry / "__init__.py").exists():
            continue
        if entry.name == root.name:
            return entry.name  # package named after the repo wins outright
        fallback = fallback or entry.name
    return fallback


def _glob_script() -> str:
    lines = [
        "import glob, os",
        f"pats = {list(CANDIDATE_GLOBS)!r}",
        "out = []",
        "for p in pats:",
        "    for m in sorted(glob.glob(p)):",
        "        if os.access(m, os.X_OK) and m not in out:",
        "            out.append(m)",
        "print(chr(10).join(out))",
    ]
    return "\n".join(lines)


def _probe_script(package: str | None) -> str:
    lines = [
        "import sys",
        "print('EXE=' + sys.executable)",
        "try:",
        "    import pytest",
        "    print('PYTEST=1')",
        "except Exception:",
        "    print('PYTEST=0')",
    ]
    if package:
        lines += [
            "try:",
            f"    import {package}",
            "    print('PKG=1')",
            "except Exception:",
            "    print('PKG=0')",
        ]
    else:
        lines.append("print('PKG=0')")
    return "\n".join(lines)


def interpreter_dir(executable: str) -> str | None:
    """Directory holding ``executable``, in the sandbox's own path flavour.

    The probe runs inside the sandbox, so a Docker run reports a POSIX path even
    when the API process is on Windows; parsing that with the host's flavour
    would silently produce no directory at all and leave PATH untouched.
    """
    if "/" not in executable and chr(92) not in executable:
        return None  # a bare name like "python3" has no directory
    if executable.startswith("/"):
        return str(PurePosixPath(executable).parent)
    return str(Path(executable).parent)


async def _run(sandbox: Sandbox, argv: list[str], timeout_s: int = 60):
    """Execute a probe command, swallowing every failure.

    A probe must never be the reason a run dies, so any exception here degrades
    to "unknown" rather than propagating.
    """
    try:
        return await sandbox.exec(argv, timeout_s=timeout_s)
    except Exception as exc:
        log.debug("environment.probe_failed", argv=argv[:2], error=str(exc))
        return None


async def candidate_interpreters(sandbox: Sandbox) -> list[str]:
    """Expand the candidate globs inside the sandbox, most specific first."""
    found: list[str] = []
    script = _glob_script()
    for launcher in CANDIDATE_NAMES:
        result = await _run(sandbox, [launcher, "-c", script])
        if result is not None and result.exit_code == 0:
            found = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            break
    for name in CANDIDATE_NAMES:
        if name not in found:
            found.append(name)  # the bare launcher is a candidate too, last
    return found


async def probe(
    sandbox: Sandbox,
    workspace_root: Path,
    *,
    package: str | None = None,
) -> RuntimeEnvironment:
    """Find the interpreter that can import the project, and report on it."""
    package = package or top_level_package(workspace_root)
    script = _probe_script(package)
    notes: list[str] = []
    fallback: RuntimeEnvironment | None = None

    for candidate in await candidate_interpreters(sandbox):
        result = await _run(sandbox, [candidate, "-c", script])
        if result is None or result.exit_code != 0:
            continue
        fields = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        executable = fields.get("EXE") or candidate
        parent = interpreter_dir(executable)
        found = RuntimeEnvironment(
            python=executable,
            bin_dir=parent,
            has_pytest=fields.get("PYTEST") == "1",
            package=package,
            imports_project=fields.get("PKG") == "1",
            probed=True,
            notes=notes,
        )
        if found.imports_project:
            notes.append(f"{executable} imports {package}")
            return found
        # Keep the first interpreter that merely runs, but keep looking for one
        # that can actually import the project.
        fallback = fallback or found

    if fallback is None:
        notes.append("no working interpreter found")
        return RuntimeEnvironment(probed=True, notes=notes)
    notes.append(f"{fallback.python} runs but could not import {package or 'the project'}")
    return fallback


async def apply_to_sandbox(sandbox: Sandbox, env: RuntimeEnvironment) -> None:
    """Put the discovered interpreter first on PATH for the rest of the run."""
    if not env.usable or not env.bin_dir:
        return
    current = await _run(
        sandbox, ["python", "-c", "import os; print(os.environ.get('PATH', ''))"]
    )
    existing = current.stdout.strip() if current is not None and current.exit_code == 0 else ""
    separator = sandbox.path_separator
    sandbox.env_overlay["PATH"] = (
        f"{env.bin_dir}{separator}{existing}" if existing else env.bin_dir
    )
    log.info("environment.path_applied", python=env.python, bin_dir=env.bin_dir)
