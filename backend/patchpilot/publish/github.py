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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..domain.enums import PublishErrorCode
from ..logging_setup import get_logger
from ..review.digest import canonical_patch, patch_digest
from .branch import ensure_pushable
from .credentials import GITHUB_API, GitHubCredentials
from .ports import (
    AuthenticatedUser,
    CheckRun,
    GitHostingError,
    PullRequestInfo,
    RepositoryInfo,
)
from .verify import WORKFLOW_DIR, changed_paths, workflow_paths

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
        return _repository(body, owner, repo)

    # -- identity and forks -------------------------------------------------
    async def get_authenticated_user(self) -> AuthenticatedUser:
        """Who the credential is.

        A GitHub App installation token is not a user and `/user` returns 403
        for it. That is reported as its own error rather than an auth failure:
        the App path is expected to have direct write access, so needing a fork
        means something is misconfigured, and saying "authentication failed"
        would send the reader after the wrong problem.
        """
        try:
            body = (await self._request("GET", "/user")).json()
        except GitHostingError as exc:
            raise GitHostingError(
                PublishErrorCode.IDENTITY_UNKNOWN,
                "Could not determine which GitHub account this credential acts "
                "as, so a fork cannot be located. A GitHub App installation "
                "token has no user account and can only publish to "
                f"repositories the App is installed on. ({exc})",
            ) from exc
        login = str(body.get("login") or "")
        return AuthenticatedUser(login=login)

    async def find_fork(
        self, *, owner: str, repo: str, user: str
    ) -> RepositoryInfo | None:
        """``user``'s fork of ``owner/repo``, if it exists.

        Looks the fork up by its conventional name first, which is one request
        instead of paging every fork of a popular repository. The parent is
        then checked, because a same-named repository owned by the user is not
        necessarily a fork of this upstream -- publishing into an unrelated
        repository of theirs would be a surprising thing to do on their behalf.
        """
        client = await self._http()
        response = await client.get(
            f"/repos/{user}/{repo}", headers=await self._headers()
        )
        if response.status_code == 404:
            return None
        _raise_for_status(response)
        candidate = _repository(response.json(), user, repo)

        upstream = f"{owner}/{repo}".lower()
        if candidate.fork and (candidate.parent_full_name or "").lower() == upstream:
            return candidate
        if not candidate.fork:
            log.info(
                "publish.fork_name_taken",
                repository=candidate.full_name,
                reason="exists but is not a fork of the upstream",
            )
        return None

    async def create_fork(self, *, owner: str, repo: str) -> RepositoryInfo:
        """Fork ``owner/repo`` to the authenticated account.

        GitHub answers 202 and copies in the background, so the repository this
        returns may not be usable yet; the caller polls.
        """
        try:
            response = await self._request("POST", f"/repos/{owner}/{repo}/forks")
        except GitHostingError as exc:
            raise GitHostingError(
                PublishErrorCode.FORK_CREATE_FAILED,
                f"GitHub refused to fork {owner}/{repo}. {exc}",
            ) from exc
        body = response.json() if response.content else {}
        full = str(body.get("full_name") or "")
        fork_owner, _, fork_repo = full.partition("/")
        return _repository(body, fork_owner or "", fork_repo or repo)

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

    async def resolve_commit(self, owner: str, repo: str, ref: str) -> str:
        body = (await self._request("GET", f"/repos/{owner}/{repo}/commits/{ref}")).json()
        full = str(body.get("sha") or "")
        if not full:
            raise GitHostingError(
                PublishErrorCode.BASE_COMMIT_UNKNOWN,
                f"{owner}/{repo} does not have a commit {ref}.",
            )
        return full

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
        """The open pull request from ``head`` into ``owner/repo``, if any.

        ``head`` arrives already qualified (``forkowner:branch``) for a fork,
        and must not be qualified a second time: ``upstream:forkowner:branch``
        matches nothing, so a retry after a lost response could never adopt the
        PR it had already opened. Git forbids ``:`` in branch names, so its
        presence reliably means the owner is already there.

        Open only. A closed PR from an earlier publication of a since-deleted
        branch of the same name is not this publication's pull request, and
        adopting it would report a PR that was never opened.
        """
        qualified = head if ":" in head else f"{owner}:{head}"
        body = (
            await self._request(
                "GET",
                f"/repos/{owner}/{repo}/pulls",
                params={"head": qualified, "state": "open"},
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
        source_owner: str | None = None,
        source_repo: str | None = None,
        protected_default_branch: str | None = None,
    ) -> str:
        """Apply the approved patch on a fresh branch and push it.

        Runs in a temporary clone that is deleted afterwards, so nothing about
        one publication can influence the next.

        The base commit is read from ``source_owner/source_repo`` and the branch
        is pushed to ``owner/repo``. They are the same repository for direct
        publication and differ for a fork, where the reviewed commit lives
        upstream and only the branch belongs to the fork.
        """
        ensure_pushable(branch, default_branch=protected_default_branch)
        token = await self._credentials.bearer_token()
        workdir = Path(tempfile.mkdtemp(prefix="patchpilot-publish-"))
        from_owner = source_owner or owner
        from_repo = source_repo or repo
        plan = _PushPlan(
            workdir=workdir,
            push_url=f"https://github.com/{owner}/{repo}.git",
            source_url=f"https://github.com/{from_owner}/{from_repo}.git",
            source_label=f"{from_owner}/{from_repo}",
            branch=branch,
            base_commit_sha=base_commit_sha,
            patch=patch,
            commit_message=commit_message,
            author_name=author_name,
            author_email=author_email,
            token=token,
            default_branch=protected_default_branch,
        )
        try:
            return await asyncio.to_thread(_apply_and_push, plan)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------- helpers


def _repository(body: dict[str, Any], owner: str, repo: str) -> RepositoryInfo:
    parent = body.get("parent")
    return RepositoryInfo(
        full_name=body.get("full_name", f"{owner}/{repo}"),
        default_branch=body.get("default_branch", "main"),
        private=bool(body.get("private", False)),
        html_url=body.get("html_url", f"https://github.com/{owner}/{repo}"),
        permissions=body.get("permissions") or {},
        fork=bool(body.get("fork", False)),
        parent_full_name=(
            str(parent.get("full_name")) if isinstance(parent, dict) and parent.get("full_name")
            else None
        ),
    )


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


@dataclass(frozen=True, slots=True)
class _PushPlan:
    """Everything one publication push needs, resolved before git starts.

    The remote URLs are fields rather than being built from ``owner/repo``
    inside the routine. That is what lets the integration tests drive the real
    production code against local bare repositories instead of GitHub -- the git
    behaviour under test is then the real thing, not a reimplementation in a
    fake.
    """

    workdir: Path
    push_url: str
    source_url: str
    source_label: str
    branch: str
    base_commit_sha: str
    patch: str
    commit_message: str
    author_name: str
    author_email: str
    token: str | None
    default_branch: str | None


def _apply_and_push(plan: _PushPlan) -> str:
    """Clone, branch, apply, verify, commit, verify again, push. Returns the SHA.

    Synchronous by design and run on a worker thread: git is a subprocess, and
    interleaving these steps would make the verification meaningless.
    """
    workdir = plan.workdir
    _git(workdir, "init", "--quiet")
    _git(workdir, "remote", "add", "origin", plan.push_url)
    # Fetch only the approved base commit, from the repository that actually has
    # it. For a fork that is upstream, not the fork: a fork created after the
    # review may not contain the reviewed commit, and syncing it would risk
    # moving the patch onto a different revision. Fetching the exact SHA from
    # upstream sidesteps fork synchronisation entirely.
    forked = plan.source_url != plan.push_url
    if forked:
        _git(workdir, "remote", "add", "upstream", plan.source_url)
        fetch_remote = "upstream"
    else:
        fetch_remote = "origin"
    try:
        _git(
            workdir, "fetch", "--quiet", "--depth", "1", fetch_remote, plan.base_commit_sha,
            token=plan.token,
        )
    except GitHostingError as exc:
        raise GitHostingError(
            PublishErrorCode.FORK_BASE_UNAVAILABLE
            if forked
            else PublishErrorCode.BASE_COMMIT_UNKNOWN,
            f"The approved base commit {plan.base_commit_sha[:12]} could not be "
            f"fetched from {plan.source_label}. It may have been garbage collected "
            "after a force-push. The patch must be regenerated and reviewed against "
            "a commit that still exists.",
        ) from exc
    _git(workdir, "checkout", "--quiet", "-b", plan.branch, "FETCH_HEAD")

    # The base as git actually resolved it, not as we asked for it. Everything
    # downstream compares against this object.
    base_sha = _git(workdir, "rev-parse", "HEAD").strip()
    if base_sha != plan.base_commit_sha:
        raise GitHostingError(
            PublishErrorCode.PUBLICATION_COMMIT_MISMATCH,
            f"The fetched base resolved to {base_sha[:12]}, but the patch was "
            f"approved against {plan.base_commit_sha[:12]}. Nothing was pushed.",
        )

    canonical = canonical_patch(plan.patch)
    patch_file = workdir / ".patchpilot-approved.patch"
    patch_file.write_text(canonical, encoding="utf-8", newline="\n")
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
    if patch_digest(applied) != patch_digest(plan.patch):
        raise GitHostingError(
            PublishErrorCode.APPLIED_DIFF_MISMATCH,
            "The diff produced on the branch does not match the approved patch. "
            "Nothing was pushed.",
        )

    _git(workdir, "-c", f"user.name={plan.author_name}", "-c", f"user.email={plan.author_email}",
         "commit", "--quiet", "-m", plan.commit_message)
    commit_sha = _git(workdir, "rev-parse", "HEAD").strip()

    approved_files = _verify_commit(plan, base_sha=base_sha, commit_sha=commit_sha)

    # Checked once more immediately before the push, against the default branch
    # of the repository being written to. For a fork that is the fork's own
    # default, which upstream's name would not have covered.
    ensure_pushable(plan.branch, default_branch=plan.default_branch)
    try:
        # No --force, ever. A rejected push means the branch already exists with
        # different content, which is someone else's work.
        _git(
            workdir, "push", "--quiet", "origin", f"{plan.branch}:refs/heads/{plan.branch}",
            token=plan.token,
        )
    except GitHostingError as exc:
        if _is_workflow_rejection(str(exc)):
            raise _explain_workflow_rejection(
                plan, base_sha=base_sha, commit_sha=commit_sha, approved_files=approved_files
            ) from exc
        raise _classify_push_failure(exc) from exc
    return commit_sha


def _verify_commit(plan: _PushPlan, *, base_sha: str, commit_sha: str) -> frozenset[str]:
    """Re-check the finished commit against the approved patch, from objects.

    The pre-commit check compared the *index*. This one reads the committed
    objects back out of the repository, which is what will actually be pushed,
    and so covers anything that could come between the two: a commit hook
    rewriting content, a partial commit, the wrong parent.

    Returns the approved file set, so the caller does not parse the patch twice.
    """
    workdir = plan.workdir

    parent = _git(workdir, "rev-parse", f"{commit_sha}^").strip()
    if parent != base_sha:
        raise GitHostingError(
            PublishErrorCode.PUBLICATION_COMMIT_MISMATCH,
            f"The publication commit is built on {parent[:12]}, not on the approved "
            f"base {base_sha[:12]}. Nothing was pushed.",
        )

    # Which files, before which bytes. A digest mismatch would catch everything
    # the file comparison catches, but it cannot say *what* went wrong, and
    # "this commit touches a workflow you did not approve" is worth saying in
    # those words. The two oracles are independent on purpose: this side reads
    # the approved patch's own headers, the other asks git.
    approved_files = changed_paths(canonical_patch(plan.patch))
    committed_files = frozenset(
        line.strip()
        # --no-renames so a rename lists both paths. The patch header names both
        # sides too, and with rename detection on git would report only the
        # destination -- which would read as a file set mismatch on any rename.
        for line in _git(
            workdir, "diff", "--name-only", "--no-renames", base_sha, commit_sha
        ).splitlines()
        if line.strip()
    )

    unapproved_workflows = workflow_paths(committed_files - approved_files)
    if unapproved_workflows:
        raise GitHostingError(
            PublishErrorCode.UNAPPROVED_WORKFLOW_CHANGE,
            "The publication commit modifies GitHub Actions workflow files that the "
            f"approved patch does not touch: {', '.join(sorted(unapproved_workflows))}. "
            "Nothing was pushed.",
        )
    if committed_files != approved_files:
        detail = []
        unexpected = sorted(committed_files - approved_files)
        missing = sorted(approved_files - committed_files)
        if unexpected:
            detail.append(f"unexpected: {', '.join(unexpected)}")
        if missing:
            detail.append(f"missing: {', '.join(missing)}")
        raise GitHostingError(
            PublishErrorCode.UNAPPROVED_FILE_CHANGE,
            "The publication commit changes a different set of files than the "
            f"approved patch ({'; '.join(detail)}). Nothing was pushed.",
        )

    # The authority. Same files is not the same content.
    committed = _git(
        workdir, "diff", "--no-color", "--no-ext-diff", "--unified=3", base_sha, commit_sha
    )
    if patch_digest(committed) != patch_digest(plan.patch):
        raise GitHostingError(
            PublishErrorCode.PUBLICATION_COMMIT_MISMATCH,
            "The committed diff does not match the approved patch. Nothing was pushed.",
        )
    return approved_files


def _is_workflow_rejection(text: str) -> bool:
    """Does this push failure look like GitHub's Actions permission check?

    The wording differs between classic and fine-grained tokens ("without
    `workflow` scope" versus "without `workflows` permission"), so this matches
    the stable part: a refusal that names workflows.
    """
    lowered = text.lower()
    # Match the phrase GitHub actually uses, not just the word "workflow" --
    # which also appears in the remote URL of any repository named after one.
    if "create or update workflow" in lowered:
        return True
    return "refusing to allow" in lowered and "workflow" in lowered


def _explain_workflow_rejection(
    plan: _PushPlan,
    *,
    base_sha: str,
    commit_sha: str,
    approved_files: frozenset[str],
) -> GitHostingError:
    """Say why a patch touching no workflow was refused for touching one.

    GitHub does not judge this branch's own commit in isolation. The commit a
    publication introduces is exactly ``apply(base, approved_patch)`` -- verified
    twice above -- so when the server names a workflow file, that commit is not
    what it is objecting to. What differs is the *tree*: a branch cut from a base
    predating a change to a workflow file still carries the older version of it,
    and measured against the destination's default branch that reads as
    modifying the file.

    The remedy is therefore about the base commit, not about permissions, and
    the message says so. Granting workflow access would make this push succeed
    while leaving PatchPilot able to rewrite CI on every future publication,
    which is the boundary worth keeping.

    Diagnosis runs only on this failure path, so a working publication pays
    nothing for it. It is read-only, and it happens after the commit is built and
    verified, so nothing it fetches can influence what was committed.
    """
    drifted = _workflow_drift(plan, commit_sha=commit_sha)
    where = plan.default_branch or "the destination's default branch"
    changes = ", ".join(sorted(approved_files)) or "no files"

    if drifted:
        return GitHostingError(
            PublishErrorCode.WORKFLOW_DRIFT,
            "GitHub refused the push because the branch differs from "
            f"{where} in a workflow file. The approved patch touches no workflow: "
            f"it changes only {changes}. The branch is based on {base_sha[:12]}, "
            f"which predates changes to {', '.join(sorted(drifted))}, so pushing it "
            "reads as reverting them. Re-approve the patch against the current head "
            f"of {where}, or publish to a repository whose default branch matches "
            "the reviewed base. PatchPilot deliberately does not request workflow "
            "permission to work around this.",
        )
    return GitHostingError(
        PublishErrorCode.WORKFLOW_DRIFT,
        "GitHub refused the push for a workflow-permission reason, but the approved "
        f"patch changes only {changes} and the publication commit was verified to "
        "match it. This normally means the branch's base predates a workflow change "
        f"on {where}. Re-approve the patch against a current base commit. PatchPilot "
        "deliberately does not request workflow permission to work around this.",
    )


def _workflow_drift(plan: _PushPlan, *, commit_sha: str) -> frozenset[str]:
    """Workflow files that differ between the branch and the push destination.

    Best-effort: if the default branch cannot be fetched, the caller still
    reports a useful error, just without naming files.
    """
    if not plan.default_branch:
        return frozenset()
    try:
        _git(
            plan.workdir, "fetch", "--quiet", "--depth", "1", "origin", plan.default_branch,
            token=plan.token,
        )
        names = _git(
            plan.workdir, "diff", "--name-only", "FETCH_HEAD", commit_sha, "--", WORKFLOW_DIR
        )
    except GitHostingError:
        return frozenset()
    return frozenset(line.strip() for line in names.splitlines() if line.strip())


def _classify_push_failure(exc: GitHostingError) -> GitHostingError:
    """Turn a raw git push failure into something the user can act on.

    A 403 here is almost always a token permission, not a broken integration:
    the credential authenticated (GitHub names the account in the message) and
    was then refused. The REST API is no help in spotting it either -- on a
    public repository `GET /repos/{owner}/{repo}` succeeds and reports the
    *owner's* permissions, so it says `push: true` for a token that cannot push
    at all. Saying so plainly saves the reader from re-checking everything that
    is already correct.
    """
    text = _scrub(str(exc))
    lowered = text.lower()

    if "403" in text or "denied to" in lowered or "permission to" in lowered:
        return GitHostingError(
            PublishErrorCode.PERMISSION_DENIED,
            "GitHub accepted the credential but refused the push. The token is "
            "missing write access to this repository's contents. For a "
            "fine-grained token, set Repository permissions -> Contents to "
            "'Read and write', and make sure this repository is in the token's "
            "selected repositories. Classic tokens need the 'repo' scope.",
        )
    if "401" in text or "invalid credentials" in lowered or "authentication failed" in lowered:
        return GitHostingError(
            PublishErrorCode.AUTH_FAILED,
            "GitHub rejected the credential. Check the token has not expired "
            "and was copied in full.",
        )
    if "shallow update not allowed" in lowered:
        return GitHostingError(
            PublishErrorCode.FORK_BASE_UNAVAILABLE,
            "The destination repository does not contain the reviewed base commit, "
            "so the branch cannot be pushed without uploading the project's entire "
            "history. This normally means the fork is not a fork of the repository "
            "being patched, or was created from a rewritten history. Sync the fork "
            "with upstream and retry.",
        )
    if "protected branch" in lowered or "pre-receive hook declined" in lowered:
        return GitHostingError(
            PublishErrorCode.PROTECTED_BRANCH,
            "The push was refused by a branch protection rule or a pre-receive hook.",
        )
    if "non-fast-forward" in lowered or "fetch first" in lowered:
        return GitHostingError(
            PublishErrorCode.BRANCH_EXISTS,
            "That branch already exists with different content. PatchPilot never "
            "force-pushes; delete or rename the branch, then retry.",
        )
    return GitHostingError(PublishErrorCode.PUSH_FAILED, text[:300])
