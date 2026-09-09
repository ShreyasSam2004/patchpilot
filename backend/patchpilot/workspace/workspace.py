"""Repository workspace.

A :class:`Workspace` owns one checkout on disk and is the *only* thing that
resolves a model-supplied path into a real filesystem path. Every read, write
and patch goes through :meth:`resolve`, which refuses anything that escapes the
root — including via ``..``, absolute paths, and symlinks that point outside.

Git is invoked through the run's :class:`~patchpilot.sandbox.base.Sandbox`, so
repository hooks and filters execute under the same limits as the tests.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import PathEscapeError, ToolError, WorkspaceError
from ..fsutil import remove_tree
from ..logging_setup import get_logger
from ..sandbox.base import ExecResult, Sandbox

log = get_logger(__name__)

#: Directories never worth listing or searching.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".tox", ".nox", ".venv", "venv", "env", "node_modules",
        ".next", "dist", "build", ".eggs", ".idea", ".vscode", "htmlcov",
        ".coverage", "site-packages", ".gradle", "target",
    }
)

BINARY_SUFFIXES: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip",
        ".tar", ".gz", ".bz2", ".xz", ".7z", ".whl", ".so", ".dll", ".dylib",
        ".exe", ".bin", ".pyc", ".pyo", ".class", ".jar", ".woff", ".woff2",
        ".ttf", ".eot", ".mp4", ".mp3", ".wav", ".sqlite", ".db",
    }
)


_ROOTED = re.compile(r"^(?:[/\\]|[A-Za-z]:[/\\]?|\\\\)")


def _is_rooted(path: str) -> bool:
    """True if ``path`` is absolute under POSIX *or* Windows conventions."""
    return bool(_ROOTED.match(path))


@dataclass(slots=True)
class GitInfo:
    remote: str | None
    head: str | None
    branch: str | None
    dirty: bool


@dataclass(slots=True)
class DiffStat:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    files: list[str] = field(default_factory=list)


#: Names that read as a throwaway reproduction script rather than a fix.
_SCRATCH_FILE = re.compile(
    r"^(repro|reproduce|debug|scratch|tmp|temp|snippet|example|check)[\w.-]*\.py$",
    re.IGNORECASE,
)


class Workspace:
    """A jailed checkout plus the git operations the agent needs."""

    def __init__(self, root: Path, sandbox: Sandbox) -> None:
        self.root = root.resolve()
        self.sandbox = sandbox
        self._files_read: set[str] = set()

    # ------------------------------------------------------------------ paths
    def resolve(self, relative: str, *, must_exist: bool = False) -> Path:
        """Resolve a repository-relative path, refusing any escape.

        A path is treated as *rooted* if it looks absolute under either POSIX or
        Windows conventions, regardless of the host OS. Without that check,
        ``/etc/passwd`` is not ``Path.is_absolute()`` on Windows and would be
        quietly rewritten to ``<root>/etc/passwd`` — no escape, but the same
        input would behave differently on Linux. Rooted paths must genuinely
        live inside the workspace or they are refused.
        """
        if relative is None:
            raise PathEscapeError("<none>")
        raw = str(relative).strip()
        if not raw or raw in {".", "./"}:
            return self.root

        if _is_rooted(raw):
            target = Path(raw).resolve()
        else:
            cleaned = raw.replace("\\", "/")
            target = (self.root / cleaned).resolve()

        if target != self.root and self.root not in target.parents:
            raise PathEscapeError(str(relative))
        if must_exist and not target.exists():
            raise ToolError(f"path not found: {self.relpath(target)}", code="not_found")
        return target

    def relpath(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    # ------------------------------------------------------------------- read
    def read_text(self, relative: str, max_bytes: int) -> tuple[str, bool]:
        """Read a file as text. Returns ``(content, truncated)``."""
        path = self.resolve(relative, must_exist=True)
        if path.is_dir():
            raise ToolError(f"{self.relpath(path)} is a directory, not a file", code="is_dir")
        if path.suffix.lower() in BINARY_SUFFIXES:
            raise ToolError(
                f"{self.relpath(path)} looks binary ({path.suffix}); refusing to read as text",
                code="binary",
            )
        raw = path.read_bytes()
        truncated = len(raw) > max_bytes
        if truncated:
            raw = raw[:max_bytes]
        if b"\x00" in raw[:4096]:
            raise ToolError(f"{self.relpath(path)} contains NUL bytes (binary)", code="binary")
        self._files_read.add(self.relpath(path))
        return raw.decode("utf-8", errors="replace"), truncated

    def write_text(self, relative: str, content: str) -> Path:
        path = self.resolve(relative)
        if path.is_dir():
            raise ToolError(f"{self.relpath(path)} is a directory", code="is_dir")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return path

    def list_dir(self, relative: str = ".", *, depth: int = 1, limit: int = 400) -> list[str]:
        """List entries below ``relative``; directories are suffixed with ``/``."""
        base = self.resolve(relative, must_exist=True)
        if base.is_file():
            return [self.relpath(base)]
        entries: list[str] = []
        base_depth = len(base.parts)
        for current_root, dirnames, filenames in os.walk(base):
            current = Path(current_root)
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            level = len(current.parts) - base_depth
            if level >= depth:
                dirnames[:] = []
            for name in dirnames:
                entries.append(self.relpath(current / name) + "/")
            for name in sorted(filenames):
                entries.append(self.relpath(current / name))
            if len(entries) >= limit:
                break
        return entries[:limit]

    def iter_source_files(self, relative: str = ".", *, suffixes: set[str] | None = None):
        """Yield candidate text files below ``relative``, skipping vendor dirs."""
        base = self.resolve(relative, must_exist=True)
        if base.is_file():
            yield base
            return
        for current_root, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in sorted(filenames):
                path = Path(current_root) / name
                if path.suffix.lower() in BINARY_SUFFIXES:
                    continue
                if suffixes and path.suffix.lower() not in suffixes:
                    continue
                yield path

    @property
    def files_read(self) -> list[str]:
        return sorted(self._files_read)

    def note_read(self, relative: str) -> None:
        self._files_read.add(relative)

    # -------------------------------------------------------------------- git
    async def git(self, *args: str, timeout_s: int = 120) -> ExecResult:
        # `safe.directory` has to be supplied with -c: git deliberately ignores
        # repository-local config for it. Without this, git refuses to operate
        # on a bind-mounted workspace whose owner differs inside the container.
        return await self.sandbox.exec(
            ["git", "-c", "safe.directory=*", *args], cwd=None, timeout_s=timeout_s
        )

    async def ensure_git_identity(self) -> None:
        """Some repos need an identity for `git stash`/`git apply --3way`."""
        await self.git("config", "user.email", "agent@patchpilot.local")
        await self.git("config", "user.name", "PatchPilot Agent")

    async def head_commit(self) -> str | None:
        result = await self.git("rev-parse", "HEAD")
        return result.stdout.strip() if result.ok else None

    async def info(self) -> GitInfo:
        remote = await self.git("config", "--get", "remote.origin.url")
        head = await self.git("rev-parse", "HEAD")
        branch = await self.git("rev-parse", "--abbrev-ref", "HEAD")
        status = await self.git("status", "--porcelain")
        return GitInfo(
            remote=remote.stdout.strip() or None if remote.ok else None,
            head=head.stdout.strip() or None if head.ok else None,
            branch=branch.stdout.strip() or None if branch.ok else None,
            dirty=bool(status.stdout.strip()) if status.ok else False,
        )

    async def _stage_all(self) -> None:
        """Stage every change except PatchPilot's own scratch files."""
        await self.git(
            "add", "-A", "--", ".",
            ":(exclude).patchpilot*",
            ":(exclude)*.pyc",
            ":(exclude)__pycache__",
        )
        await self._drop_scratch_additions()

    async def _drop_scratch_additions(self) -> list[str]:
        """Unstage reproduction scripts the agent dropped in the repository root.

        Agents write a script to reproduce the bug, and when they cannot write
        outside the workspace they write it inside one. Both observed cases hurt:
        one instance shipped `repro_test2.py` alongside a real fix, and another
        submitted `repro_debug.py` as its *entire* patch -- graded, of course, as
        unresolved.

        Narrow on purpose. Only newly *added* files, only in the repository root,
        only scratch-shaped names. A new module inside a package is left alone,
        because that can be a genuine fix, and modifications to a tracked file of
        any name are never touched.
        """
        listing = await self.git("diff", "--cached", "--name-status")
        if not listing.ok:
            return []
        dropped: list[str] = []
        for line in listing.stdout.splitlines():
            parts = line.split(chr(9))
            if len(parts) < 2 or parts[0].strip() != "A":
                continue
            path = parts[1].strip()
            if "/" in path or chr(92) in path:
                continue
            if _SCRATCH_FILE.match(path):
                dropped.append(path)
        for path in dropped:
            await self.git("reset", "--quiet", "HEAD", "--", path)
        if dropped:
            log.info("workspace.scratch_excluded_from_patch", files=dropped)
        return dropped

    async def diff(self) -> str:
        """The full working-tree diff, including new files.

        New files are picked up by staging with ``git add -A`` first (intent to
        add would be enough, but staging keeps ``git diff --cached`` simple and
        matches what SWE-bench expects a prediction patch to contain).
        """
        await self._stage_all()
        result = await self.git("diff", "--cached", "--no-color", "--no-ext-diff", "--unified=3")
        if not result.ok:
            log.warning("workspace.diff_failed", stderr=result.stderr[:400])
            return ""
        return result.stdout

    async def diff_stat(self) -> DiffStat:
        await self._stage_all()
        result = await self.git("diff", "--cached", "--numstat")
        stat = DiffStat()
        if not result.ok:
            return stat
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            stat.files.append(path)
            stat.files_changed += 1
            stat.insertions += int(added) if added.isdigit() else 0
            stat.deletions += int(removed) if removed.isdigit() else 0
        return stat

    async def reset_hard(self, ref: str = "HEAD") -> ExecResult:
        """Discard every working-tree change, including untracked files."""
        await self.git("reset", "--quiet", ref)
        await self.git("clean", "-fdq", "-e", ".patchpilot*")
        return await self.git("checkout", "--force", "--", ".")

    async def apply_patch(self, diff_text: str) -> ExecResult:
        """Apply a unified diff, trying progressively more forgiving strategies."""
        if not diff_text.strip():
            raise ToolError("empty patch", code="empty_patch")
        if not diff_text.endswith("\n"):
            diff_text += "\n"

        patch_file = self.root / ".patchpilot_patch.diff"
        patch_file.write_text(diff_text, encoding="utf-8", newline="\n")
        try:
            attempts = [
                ["apply", "--verbose", "--whitespace=nowarn", ".patchpilot_patch.diff"],
                ["apply", "--3way", "--whitespace=nowarn", ".patchpilot_patch.diff"],
                ["apply", "-C1", "--whitespace=fix", ".patchpilot_patch.diff"],
            ]
            last: ExecResult | None = None
            for args in attempts:
                result = await self.git(*args)
                if result.ok:
                    return result
                last = result
            assert last is not None
            return last
        finally:
            patch_file.unlink(missing_ok=True)

    # ------------------------------------------------------------- lifecycle
    def destroy(self) -> None:
        remove_tree(self.root)


#: Git settings applied to the *provisioning* clone/fetch, which runs on the
#: host. Fetching bytes is PatchPilot's own trusted operation, but the remote
#: still gets to influence git's behaviour, so nothing that can execute is left
#: enabled: no hooks, no filesystem monitor, no credential helpers, and none of
#: the transports that shell out.
_PROVISION_GIT_CONFIG: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.file.allow=never",
    "-c", "advice.detachedHead=false",
)


async def prepare_workspace(
    root: Path,
    sandbox: Sandbox,
    *,
    repo_url: str | None = None,
    local_path: Path | None = None,
    base_commit: str | None = None,
    provisioner: Sandbox | None = None,
) -> Workspace:
    """Materialise a checkout at ``root`` from a URL or a local directory.

    Provisioning is split in two, because the two halves have different needs:

    * **clone / fetch** run in ``provisioner`` — they need network access, and
      the Docker sandbox is deliberately started with ``--network none``. This
      is PatchPilot's own operation, not repository code, and it runs with
      hooks, filters and credential helpers disabled.
    * **populating the working tree** runs in ``sandbox`` — checking out repo
      content can trigger ``.gitattributes`` smudge filters, so it belongs
      inside whatever isolation the run was configured with.

    ``provisioner`` defaults to ``sandbox``, which is correct for the local
    sandbox (it already has network) and for tests.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    workspace = Workspace(root, sandbox)
    network = provisioner or sandbox

    if local_path is not None:
        source = local_path.resolve()
        if not source.exists():
            raise WorkspaceError(f"local repository not found: {source}")
        if root.exists():
            remove_tree(root)
        shutil.copytree(
            source,
            root,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".venv", "node_modules"),
            dirs_exist_ok=False,
        )
    elif repo_url:
        root.mkdir(parents=True, exist_ok=True)
        # --no-checkout: fetch the objects here, but let the working tree be
        # populated inside the run's sandbox further down.
        clone_args = [*_PROVISION_GIT_CONFIG, "clone", "--quiet", "--no-checkout"]
        if base_commit is None:
            clone_args += ["--depth", "50"]
        clone_args += [repo_url, "."]
        result = await network.exec(["git", *clone_args], timeout_s=1800)
        if not result.ok:
            raise WorkspaceError(
                f"git clone failed: {(result.stderr or result.stdout).strip()[:500]}"
            )
    else:
        raise WorkspaceError("either repo_url or local_path is required")

    if not (root / ".git").exists():
        await workspace.git("init", "--quiet")
        await workspace.ensure_git_identity()
        await workspace.git("add", "-A")
        await workspace.git("commit", "--quiet", "-m", "patchpilot: baseline", "--allow-empty")
    else:
        await workspace.ensure_git_identity()

    if repo_url:
        if base_commit:
            fetch = await network.exec(
                ["git", *_PROVISION_GIT_CONFIG, "fetch", "--quiet", "origin", base_commit],
                timeout_s=1800,
            )
            if not fetch.ok:
                await network.exec(
                    ["git", *_PROVISION_GIT_CONFIG, "fetch", "--quiet", "--unshallow"],
                    timeout_s=1800,
                )

        # Populate the working tree inside the run's sandbox. `reset --hard`
        # rather than `checkout` because the clone was made --no-checkout, so
        # both the index and the tree still need filling.
        target = base_commit or "HEAD"
        populated = await workspace.git("reset", "--hard", "--quiet", target, timeout_s=600)
        if not populated.ok:
            raise WorkspaceError(
                f"could not check out {target}: "
                f"{(populated.stderr or populated.stdout).strip()[:400]}"
            )

    # Anything already dirty would pollute the final diff.
    await workspace.git("reset", "--quiet")
    log.info("workspace.ready", root=str(root), base_commit=base_commit)
    return workspace
