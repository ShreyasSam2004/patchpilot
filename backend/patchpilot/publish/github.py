"""GitHub adapter for :class:`~patchpilot.publish.ports.GitHostingPort`.

Two mechanisms, chosen for what each is actually good at:

* **REST** for metadata, branch heads, pull requests and checks.
* **The git CLI, in a throwaway clone**, for applying the approved patch.

The second needs justifying, because the Git Data API could create a tree
without touching disk. It cannot apply a *diff*: it takes whole file contents,
so using it would mean reconstructing every changed file from the patch and
hoping the reconstruction is faithful. `git apply` is the same program that
produced the diff, and afterwards `git diff` re-derives it, which gives us a
real before/after comparison against the approved bytes. If they differ we stop
and publish nothing -- and that check is the entire integrity story.

Credentials appear only in an in-memory `Authorization` header or an ephemeral
`http.extraheader` argument. They are never written into a remote URL, a config
file, an event, or a log line.
"""

from __future__ import annotations

import asyncio
import base64
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..domain.enums import PublishErrorCode
from ..logging_setup import get_logger
from ..review.digest import canonical_patch, patch_digest
from .branch import ensure_pushable
from .credentials import GITHUB_API, GitHubCredentials
from .ports import CheckRun, GitHostingError, PullRequestInfo, RepositoryInfo

log = get_logger(__name__)

_GIT_TIMEOUT_S = 300


class GitHubAdapter:
    """Talks to github.com. The only module in the codebase that does."""

    def __init__(self, settings: Settings, credentials: GitHubCredentials | None = None) -> None:
        self._settings = settings
        self._credentials = credentials or GitHubCredentials(settings)
        self._client: httpx.AsyncClient | None = None

    # -- plumbing -----------------------------------------------------------
    @property
    def configured(self) -> bool:
        return self._credentials.configured

    def describe(self) -> dict[str, object]:
        return self._credentials.describe()

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=GITHUB_API, timeout=30)
        return self._client

    async def _headers(self) -> dict[str, str]:
        token = await self._credentials.bearer_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = await self._http()
        try:
            response = await client.request(
                method, path, headers=await self._headers(), **kwargs
            )
        except httpx.HTTPError as exc:
            raise GitHostingError(
                PublishErrorCode.UNKNOWN, f"Could not reach GitHub: {exc}"
            ) from exc
        _raise_for_status(response)
        return response

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- reads --------------------------------------------------------------
    async def get_repository(self, owner: str, repo: str) -> RepositoryInfo:
        body = (await self._request("GET", f"/repos/{owner}/{repo}")).json()
        return RepositoryInfo(
            full_name=body.get("full_name", f"{owner}/{repo}"),
            default_branch=body.get("default_branch", "main"),
            private=bool(body.get("private", False)),
            html_url=body.get("html_url", f"https://github.com/{owner}/{repo}"),
            permissions=body.get("permissions") or {},
        )

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        body = (
            await self._request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}")
        ).json()
        return str((body.get("object") or {}).get("sha", ""))

    async def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        client = await self._http()
        response = await client.get(
            f"/repos/{owner}/{repo}/git/ref/heads/{branch}", headers=await self._headers()
        )
        if response.status_code == 404:
            return False
        _raise_for_status(response)
        return True

    async def get_check_runs(self, *, owner: str, repo: str, ref: str) -> list[CheckRun]:
        try:
            body = (
                await self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs")
            ).json()
        except GitHostingError:
            # Checks are decoration; never fail a publication because CI status
            # could not be read.
            return []
        return [
            CheckRun(
                name=item.get("name", "check"),
                status=item.get("status", "queued"),
                conclusion=item.get("conclusion"),
                url=item.get("html_url"),
            )
            for item in body.get("check_runs", [])
        ]

    # -- pull requests ------------------------------------------------------
    async def find_pull_request(
        self, *, owner: str, repo: str, head: str
    ) -> PullRequestInfo | None:
        body = (
            await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls",
                params={"head": f"{owner}:{head}", "state": "all"},
            )
        ).json()
        if not body:
            return None
        return _pull_request(body[0])

    async def get_pull_request(self, *, owner: str, repo: str, number: int) -> PullRequestInfo:
        body = (await self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")).json()
        return _pull_request(body)

    async def create_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> PullRequestInfo:
        try:
            response = await self._request(
                "POST",
                f"/repos/{owner}/{repo}/pulls",
                json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
            )
        except GitHostingError as exc:
            if exc.code is PublishErrorCode.UNKNOWN:
                raise GitHostingError(
                    PublishErrorCode.PR_CREATE_FAILED, str(exc)
                ) from exc
            raise
        return _pull_request(response.json())

    # -- the write that matters ---------------------------------------------
    async def push_branch(
        self,
        *,
        owner: str,
        repo: str,
        branch: str,
        base_commit_sha: str,
        patch: str,
        commit_message: str,
        author_name: str,
        author_email: str,
    ) -> str:
        """Apply the approved patch on a fresh branch and push it.

        Runs in a temporary clone that is deleted afterwards, so nothing about
        one publication can influence the next.
        """
        ensure_pushable(branch)
        token = await self._credentials.bearer_token()
        workdir = Path(tempfile.mkdtemp(prefix="patchpilot-publish-"))
        try:
            return await asyncio.to_thread(
                _apply_and_push,
                workdir,
                owner,
                repo,
                branch,
                base_commit_sha,
                patch,
                commit_message,
                author_name,
                author_email,
                token,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------- helpers


def _pull_request(body: dict[str, Any]) -> PullRequestInfo:
    head = body.get("head") or {}
    base = body.get("base") or {}
    return PullRequestInfo(
        number=int(body.get("number") or 0),
        url=str(body.get("html_url", "")),
        state=str(body.get("state", "open")),
        head_branch=str(head.get("ref", "")) if isinstance(head, dict) else "",
        base_branch=str(base.get("ref", "")) if isinstance(base, dict) else "",
        draft=bool(body.get("draft", False)),
    )


def _raise_for_status(response: httpx.Response) -> None:
    """Map a GitHub response onto an actionable, secret-free error."""
    if response.status_code < 400:
        return
    code = {
        401: PublishErrorCode.AUTH_FAILED,
        403: PublishErrorCode.PERMISSION_DENIED,
        404: PublishErrorCode.REPOSITORY_UNKNOWN,
        422: PublishErrorCode.PR_CREATE_FAILED,
    }.get(response.status_code, PublishErrorCode.UNKNOWN)

    if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
        raise GitHostingError(
            PublishErrorCode.RATE_LIMITED,
            "GitHub rate limit reached. Retry publication once it resets.",
        )

    detail = ""
    try:
        body = response.json()
        detail = str(body.get("message", ""))[:200]
    except Exception:
        detail = ""
    raise GitHostingError(code, f"GitHub returned {response.status_code}. {detail}".strip())


def _git(workdir: Path, *args: str, token: str | None = None) -> str:
    """Run git, keeping any credential out of argv-visible places we persist.

    The token is passed via `-c http.extraheader`, which lives only in this
    process's argument list for the duration of the call -- never written to
    `.git/config`, a remote URL, or anything we log.
    """
    import subprocess

    prefix: list[str] = ["git"]
    if token:
        header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        prefix += ["-c", f"http.extraheader=Authorization: Basic {header}"]
    completed = subprocess.run(
        [*prefix, *args],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
    )
    if completed.returncode != 0:
        stderr = _scrub(completed.stderr or completed.stdout)
        raise GitHostingError(
            PublishErrorCode.UNKNOWN, f"git {args[0]} failed: {stderr[:400]}"
        )
    return completed.stdout


def _scrub(text: str) -> str:
    """Remove anything token-shaped before an error reaches a log or a user."""
    import re

    cleaned = re.sub(r"(gh[pousr]_[A-Za-z0-9]{10,})", "***", text)
    cleaned = re.sub(r"(Authorization:\s*\S+)", "Authorization: ***", cleaned)
    return re.sub(r"(https://)[^@\s/]+@", r"\1***@", cleaned)


def _apply_and_push(
    workdir: Path,
    owner: str,
    repo: str,
    branch: str,
    base_commit_sha: str,
    patch: str,
    commit_message: str,
    author_name: str,
    author_email: str,
    token: str,
) -> str:
    """Clone, branch, apply, verify, commit, push. Returns the commit SHA.

    Synchronous by design and run on a worker thread: git is a subprocess, and
    interleaving these steps would make the verification meaningless.
    """
    url = f"https://github.com/{owner}/{repo}.git"

    _git(workdir, "init", "--quiet")
    _git(workdir, "remote", "add", "origin", url)
    # Fetch only the approved base commit. The branch is created from the SHA
    # the patch was reviewed against, never from whatever the default branch
    # happens to point at now -- that is what keeps an approved patch applying
    # to the tree it was approved for.
    _git(workdir, "fetch", "--quiet", "--depth", "1", "origin", base_commit_sha, token=token)
    _git(workdir, "checkout", "--quiet", "-b", branch, "FETCH_HEAD")

    patch_file = workdir / ".patchpilot-approved.patch"
    patch_file.write_text(canonical_patch(patch), encoding="utf-8", newline="\n")
    try:
        _git(workdir, "apply", "--whitespace=nowarn", str(patch_file))
    except GitHostingError as exc:
        raise GitHostingError(
            PublishErrorCode.PATCH_APPLY_FAILED,
            "The approved patch did not apply to the base commit. "
            f"{str(exc).split(':', 1)[-1].strip()[:200]}",
        ) from exc
    patch_file.unlink(missing_ok=True)

    # Re-derive the diff from the working tree and compare it with what was
    # approved. This is the integrity check the whole design exists for: if the
    # bytes differ for any reason, nothing is pushed.
    _git(workdir, "add", "-A")
    applied = _git(workdir, "diff", "--cached", "--no-color", "--no-ext-diff", "--unified=3")
    if patch_digest(applied) != patch_digest(patch):
        raise GitHostingError(
            PublishErrorCode.APPLIED_DIFF_MISMATCH,
            "The diff produced on the branch does not match the approved patch. "
            "Nothing was pushed.",
        )

    _git(workdir, "-c", f"user.name={author_name}", "-c", f"user.email={author_email}",
         "commit", "--quiet", "-m", commit_message)
    commit_sha = _git(workdir, "rev-parse", "HEAD").strip()

    ensure_pushable(branch)
    try:
        # No --force, ever. A rejected push means the branch already exists with
        # different content, which is someone else's work.
        _git(workdir, "push", "--quiet", "origin", f"{branch}:refs/heads/{branch}", token=token)
    except GitHostingError as exc:
        raise GitHostingError(PublishErrorCode.PUSH_FAILED, _scrub(str(exc))[:300]) from exc
    return commit_sha
