"""The port the publication service talks to.

The service knows about repositories, branches and pull requests; it does not
know that GitHub exists. Everything provider-shaped -- REST paths, status
codes, rate-limit headers, token minting -- lives behind this interface in
:mod:`patchpilot.publish.github`.

That boundary is what makes the workflow testable without a network: the tests
drive a recording double through the identical protocol, so the ordering and
idempotency guarantees are exercised for real rather than mocked at the HTTP
layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..domain.enums import PublishErrorCode
from ..errors import PatchPilotError


class GitHostingError(PatchPilotError):
    """A hosting-provider operation failed.

    Carries a classified :class:`PublishErrorCode` so the UI can say something
    actionable. The message is written to be safe to display: adapters must not
    put tokens, authenticated URLs or raw provider bodies in here.
    """

    def __init__(self, code: PublishErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    full_name: str
    default_branch: str
    private: bool
    html_url: str
    permissions: dict[str, bool] = field(default_factory=dict)

    @property
    def can_push(self) -> bool:
        """Whether the credential may write. Unknown permissions read as True:
        GitHub App installation tokens do not report a permissions block, and
        refusing to try would make the App path unusable. A real lack of access
        surfaces as PERMISSION_DENIED on the push itself."""
        return self.permissions.get("push", True)


@dataclass(frozen=True, slots=True)
class PullRequestInfo:
    number: int
    url: str
    state: str
    head_branch: str
    base_branch: str
    draft: bool = False


@dataclass(frozen=True, slots=True)
class CheckRun:
    name: str
    status: str
    """queued | in_progress | completed"""
    conclusion: str | None
    """success | failure | neutral | cancelled | timed_out | action_required"""
    url: str | None = None


@runtime_checkable
class GitHostingPort(Protocol):
    """Read/write operations PatchPilot needs from a git host.

    Note what is absent, deliberately: there is no merge, no branch-protection
    change, no review submission, no settings write. An operation that does not
    exist on the port cannot be reached by any caller, which is a stronger
    guarantee than a policy saying we will not call it.
    """

    @property
    def configured(self) -> bool:
        """False when no credentials are set. The app stays usable; publication
        is simply unavailable and says so."""
        ...

    async def get_repository(self, owner: str, repo: str) -> RepositoryInfo: ...

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        """Commit SHA a branch points at. Raises if the branch is absent."""
        ...

    async def branch_exists(self, owner: str, repo: str, branch: str) -> bool: ...

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
        """Create ``branch`` at ``base_commit_sha``, apply ``patch``, push.

        Returns the new commit SHA. Implementations must apply the patch
        verbatim and verify the resulting diff before pushing: the point of the
        whole workflow is that what lands is what was approved.
        """
        ...

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
    ) -> PullRequestInfo: ...

    async def find_pull_request(
        self, *, owner: str, repo: str, head: str
    ) -> PullRequestInfo | None:
        """An open PR for this branch, if one already exists.

        Consulted before creating, so a retry after a lost response adopts the
        existing pull request instead of opening a second one.
        """
        ...

    async def get_pull_request(
        self, *, owner: str, repo: str, number: int
    ) -> PullRequestInfo: ...

    async def get_check_runs(self, *, owner: str, repo: str, ref: str) -> list[CheckRun]: ...

    async def close(self) -> None: ...


__all__ = [
    "Any",
    "CheckRun",
    "GitHostingError",
    "GitHostingPort",
    "PullRequestInfo",
    "RepositoryInfo",
]
