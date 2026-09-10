"""Choosing where an approved patch gets pushed.

Two ways a pull request can reach an upstream repository:

* **DIRECT** -- the credential can push to upstream, so the branch is created
  there. This is the original path and is unchanged.
* **FORK** -- the credential cannot, so the branch is created in the
  authenticated user's fork and the pull request is opened *from* that fork
  *against* upstream. This is how every outside contribution to a project you
  do not own has ever worked.

Two decisions in here are worth stating plainly, because both could reasonably
have gone the other way.

**Permissions are read, not discovered by failing.** It would be simpler to try
a direct push and fall back to a fork on 403. It would also mean the normal path
for any external repository is an error, which makes real errors unreadable, and
it leaves a half-made branch behind when the push fails after the branch call.
So the strategy is decided from what GitHub reports before anything is written.

**Unknown permissions mean DIRECT.** A GitHub App installation token gets no
``permissions`` block, and the pre-existing :attr:`RepositoryInfo.can_push`
already reads absence as "assume yes". Preserving that is deliberate: the App
path is the production path, forking every publication on it would be wrong, and
a genuine lack of access still surfaces as ``PERMISSION_DENIED`` on the push
with nothing published.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..domain.enums import PublicationStrategy, PublishErrorCode
from ..logging_setup import get_logger
from .ports import GitHostingError, GitHostingPort, RepositoryInfo

log = get_logger(__name__)

#: How long to wait for GitHub to finish creating a fork. Forks of small
#: repositories are usually usable within a second or two; large ones take
#: longer. Bounded on purpose -- a publication that hangs forever is worse than
#: one that fails with something a person can act on.
FORK_READY_ATTEMPTS = 12
FORK_READY_DELAY_S = 2.0


@dataclass(frozen=True, slots=True)
class PublicationTarget:
    """Where the branch goes, and what the pull request will say.

    ``upstream_*`` is always the repository the pull request targets, whichever
    strategy is in play. ``head_*`` is where the branch physically lives -- the
    same repository for DIRECT, the fork for FORK.
    """

    strategy: PublicationStrategy
    upstream_owner: str
    upstream_repo: str
    upstream_default_branch: str
    head_owner: str
    head_repo: str
    head_default_branch: str
    """The default branch *of the repository being written to*. Passed to the
    branch guard so a fork's default is refused too, not just upstream's."""

    @property
    def is_fork(self) -> bool:
        return self.strategy is PublicationStrategy.FORK

    def head_ref(self, branch: str) -> str:
        """What GitHub wants in a pull request's ``head`` field.

        A cross-repository pull request must qualify the branch with its owner:
        an unqualified name is looked for in the *target* repository, where a
        fork's branch does not exist. Direct publication stays unqualified so
        the existing behaviour is byte-identical.
        """
        return f"{self.head_owner}:{branch}" if self.is_fork else branch

    def describe(self) -> str:
        if self.is_fork:
            return (
                f"fork publication: {self.head_owner}/{self.head_repo} -> "
                f"{self.upstream_owner}/{self.upstream_repo}"
            )
        return f"direct publication: {self.upstream_owner}/{self.upstream_repo}"


async def select_target(
    host: GitHostingPort,
    *,
    owner: str,
    repo: str,
) -> PublicationTarget:
    """Decide DIRECT or FORK for ``owner/repo``, resolving a fork if needed."""
    upstream = await host.get_repository(owner, repo)

    if upstream.can_push:
        return PublicationTarget(
            strategy=PublicationStrategy.DIRECT,
            upstream_owner=owner,
            upstream_repo=repo,
            upstream_default_branch=upstream.default_branch,
            head_owner=owner,
            head_repo=repo,
            head_default_branch=upstream.default_branch,
        )

    user = await host.get_authenticated_user()
    if not user.login:
        raise GitHostingError(
            PublishErrorCode.IDENTITY_UNKNOWN,
            "GitHub did not identify the authenticated account, so there is no "
            "fork to publish from.",
        )

    fork = await resolve_fork(host, owner=owner, repo=repo, user=user.login)
    return PublicationTarget(
        strategy=PublicationStrategy.FORK,
        upstream_owner=owner,
        upstream_repo=repo,
        upstream_default_branch=upstream.default_branch,
        head_owner=fork.owner,
        head_repo=fork.name,
        head_default_branch=fork.default_branch,
    )


async def resolve_fork(
    host: GitHostingPort,
    *,
    owner: str,
    repo: str,
    user: str,
    attempts: int = FORK_READY_ATTEMPTS,
    delay_s: float = FORK_READY_DELAY_S,
) -> RepositoryInfo:
    """The user's fork of ``owner/repo``, reused if present and created if not.

    Reuse comes first and is the common case on a retry: a publication that
    failed after creating the fork must not ask for a second one.
    """
    existing = await host.find_fork(owner=owner, repo=repo, user=user)
    if existing is not None:
        log.info("publish.fork_reused", fork=existing.full_name, upstream=f"{owner}/{repo}")
        return existing

    try:
        created = await host.create_fork(owner=owner, repo=repo)
    except GitHostingError as exc:
        if exc.code in {PublishErrorCode.FORK_CREATE_FAILED, PublishErrorCode.FORK_NOT_READY}:
            raise
        raise GitHostingError(
            PublishErrorCode.FORK_CREATE_FAILED,
            f"Could not fork {owner}/{repo}: {exc}",
        ) from exc

    ready = await wait_until_ready(
        host, owner=created.owner, repo=created.name, attempts=attempts, delay_s=delay_s
    )
    log.info("publish.fork_created", fork=ready.full_name, upstream=f"{owner}/{repo}")
    return ready


async def wait_until_ready(
    host: GitHostingPort,
    *,
    owner: str,
    repo: str,
    attempts: int = FORK_READY_ATTEMPTS,
    delay_s: float = FORK_READY_DELAY_S,
) -> RepositoryInfo:
    """Poll until a freshly created fork answers, or give up with a clear error.

    GitHub returns 202 from the fork call and finishes the copy in the
    background, so the repository can 404 for a few seconds afterwards. Bounded
    attempts, and the failure names the fork so a person can look at it.
    """
    last: GitHostingError | None = None
    for attempt in range(max(1, attempts)):
        try:
            return await host.get_repository(owner, repo)
        except GitHostingError as exc:
            last = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(delay_s)

    raise GitHostingError(
        PublishErrorCode.FORK_NOT_READY,
        f"The fork {owner}/{repo} was requested but did not become available "
        f"after {attempts} attempts. GitHub creates forks in the background; "
        "retry publication in a moment."
        + (f" Last response: {last}" if last else ""),
    )


__all__ = [
    "FORK_READY_ATTEMPTS",
    "FORK_READY_DELAY_S",
    "PublicationTarget",
    "resolve_fork",
    "select_target",
    "wait_until_ready",
]
