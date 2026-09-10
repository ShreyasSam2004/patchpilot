"""Publishing an approved patch: integrity, idempotency, and the merge boundary.

Driven through a recording double that implements
:class:`~patchpilot.publish.ports.GitHostingPort`. The real service, the real
state machine and a real database are exercised; only github.com is replaced.
That is the point of the port: the ordering guarantees are testable without a
network, so they are actually tested.
"""

from __future__ import annotations

import pytest

from patchpilot.agent.result import AgentResult, TestRunRecord
from patchpilot.db.models import Run
from patchpilot.domain.enums import (
    PublicationStep,
    PublishErrorCode,
    ReviewAction,
    ReviewState,
    RunState,
)
from patchpilot.publish import service as publish_service
from patchpilot.publish.branch import (
    ProtectedBranchError,
    branch_name,
    collision_name,
    ensure_pushable,
)
from patchpilot.publish.identity import parse_repository
from patchpilot.publish.ports import (
    AuthenticatedUser,
    CheckRun,
    GitHostingError,
    GitHostingPort,
    PullRequestInfo,
    RepositoryInfo,
)
from patchpilot.review import service as review_service

BASE_SHA = "a" * 40
DRIFTED_SHA = "b" * 40
UPSTREAM_OWNER = "external-owner"
PATCH = (
    "diff --git a/sqlparse/sql.py b/sqlparse/sql.py\n"
    "--- a/sqlparse/sql.py\n"
    "+++ b/sqlparse/sql.py\n"
    "@@ -18,8 +18,9 @@\n"
    "-        dot_idx, _ = self.token_next_by(m=(T.Punctuation, '.'))\n"
    "+        dot_idx = self._last_dot_index()\n"
)


class FakeHost:
    """A recording GitHostingPort. Notably has no merge operation to record."""

    def __init__(
        self,
        *,
        configured: bool = True,
        default_branch: str = "main",
        head: str = BASE_SHA,
        existing_branches: set[str] | None = None,
        fail_push: GitHostingError | None = None,
        fail_pr: GitHostingError | None = None,
        can_push: bool = True,
        user: str = "test-user",
        existing_forks: set[str] | None = None,
        fork_ready_after: int = 0,
        fail_fork: GitHostingError | None = None,
        fork_default_branch: str | None = None,
    ) -> None:
        self._configured = configured
        self.default_branch = default_branch
        self.head = head
        self.existing_branches = existing_branches or set()
        self.fail_push = fail_push
        self.fail_pr = fail_pr
        self.calls: list[str] = []
        self.pushed: list[dict[str, str]] = []
        self.pull_requests: dict[str, PullRequestInfo] = {}
        self._next_pr = 842

        # -- fork behaviour --------------------------------------------------
        self.can_push = can_push
        """Whether the credential may push to *upstream*. False drives FORK."""
        self.user = user
        self.existing_forks = existing_forks or set()
        """Full names, e.g. {"test-user/example-project"}, that already exist."""
        self.fork_ready_after = fork_ready_after
        """How many get_repository calls a freshly created fork 404s for, so
        GitHub's asynchronous fork creation can be exercised deterministically."""
        self.fail_fork = fail_fork
        self.fork_default_branch = fork_default_branch or default_branch
        self.forks_created: list[str] = []
        self._pending_ready: dict[str, int] = {}

    def _is_fork(self, owner: str, repo: str) -> bool:
        return f"{owner}/{repo}" in self.existing_forks

    @property
    def configured(self) -> bool:
        return self._configured

    async def get_repository(self, owner: str, repo: str) -> RepositoryInfo:
        self.calls.append("get_repository")
        full = f"{owner}/{repo}"

        remaining = self._pending_ready.get(full, 0)
        if remaining > 0:
            self._pending_ready[full] = remaining - 1
            raise GitHostingError(PublishErrorCode.REPOSITORY_UNKNOWN, "not ready yet")

        if self._is_fork(owner, repo):
            return RepositoryInfo(
                full_name=full,
                default_branch=self.fork_default_branch,
                private=False,
                html_url=f"https://github.com/{full}",
                permissions={"push": True},
                fork=True,
                parent_full_name=f"{UPSTREAM_OWNER}/{repo}",
            )
        return RepositoryInfo(
            full_name=full,
            default_branch=self.default_branch,
            private=False,
            html_url=f"https://github.com/{full}",
            permissions={"push": self.can_push},
        )

    async def get_authenticated_user(self) -> AuthenticatedUser:
        self.calls.append("get_authenticated_user")
        return AuthenticatedUser(login=self.user)

    async def find_fork(self, *, owner: str, repo: str, user: str) -> RepositoryInfo | None:
        self.calls.append("find_fork")
        if f"{user}/{repo}" not in self.existing_forks:
            return None
        return await self.get_repository(user, repo)

    async def create_fork(self, *, owner: str, repo: str) -> RepositoryInfo:
        self.calls.append("create_fork")
        if self.fail_fork is not None:
            raise self.fail_fork
        full = f"{self.user}/{repo}"
        self.forks_created.append(full)
        self.existing_forks.add(full)
        if self.fork_ready_after:
            self._pending_ready[full] = self.fork_ready_after
        return RepositoryInfo(
            full_name=full,
            default_branch=self.fork_default_branch,
            private=False,
            html_url=f"https://github.com/{full}",
            permissions={"push": True},
            fork=True,
            parent_full_name=f"{owner}/{repo}",
        )

    async def get_branch_head(self, owner: str, repo: str, branch: str) -> str:
        self.calls.append("get_branch_head")
        return self.head

    async def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        self.calls.append("branch_exists")
        return branch in self.existing_branches

    async def push_branch(self, **kwargs: object) -> str:
        self.calls.append("push_branch")
        if self.fail_push is not None:
            raise self.fail_push
        self.pushed.append({str(k): str(v) for k, v in kwargs.items()})
        return "c" * 40

    async def find_pull_request(self, *, owner: str, repo: str, head: str):
        self.calls.append("find_pull_request")
        return self.pull_requests.get(head)

    async def create_pull_request(self, **kwargs: object) -> PullRequestInfo:
        self.calls.append("create_pull_request")
        if self.fail_pr is not None:
            raise self.fail_pr
        head = str(kwargs["head"])
        pr = PullRequestInfo(
            number=self._next_pr,
            url=f"https://github.com/x/y/pull/{self._next_pr}",
            state="open",
            head_branch=head,
            base_branch=str(kwargs["base"]),
        )
        self._next_pr += 1
        self.pull_requests[head] = pr
        return pr

    async def get_pull_request(self, *, owner: str, repo: str, number: int) -> PullRequestInfo:
        self.calls.append("get_pull_request")
        for pr in self.pull_requests.values():
            if pr.number == number:
                return pr
        raise GitHostingError(PublishErrorCode.UNKNOWN, "no such pull request")

    async def get_check_runs(self, *, owner: str, repo: str, ref: str) -> list[CheckRun]:
        self.calls.append("get_check_runs")
        return [
            CheckRun(name="lint", status="completed", conclusion="success"),
            CheckRun(name="unit-tests", status="in_progress", conclusion=None),
        ]

    async def close(self) -> None:
        return None


def test_the_fake_satisfies_the_port() -> None:
    assert isinstance(FakeHost(), GitHostingPort)


def test_the_port_offers_no_way_to_merge() -> None:
    """The boundary is structural: absent operations cannot be called."""
    surface = set(dir(GitHostingPort))
    for forbidden in ("merge", "merge_pull_request", "update_branch_protection",
                      "submit_review", "approve_pull_request", "delete_branch"):
        assert forbidden not in surface


async def _ready_run(session, settings, *, run_id: str = "pub-1", patch: str = PATCH) -> Run:
    run = Run(
        id=run_id,
        code=run_id[-4:].upper(),
        repository="andialbrecht/sqlparse",
        repo_url="https://github.com/andialbrecht/sqlparse",
        repo_provider="github",
        repo_owner="andialbrecht",
        repo_name="sqlparse",
        base_commit=BASE_SHA,
        base_branch="main",
        issue="get_real_name is wrong",
        issue_title="get_real_name returns the wrong component",
        issue_url="https://github.com/andialbrecht/sqlparse/issues/332",
        model="claude-opus-5",
        state=str(RunState.RESOLVED),
        review_state=str(ReviewState.RUNNING),
    )
    session.add(run)
    await session.flush()

    result = AgentResult(run_id=run.id, state=RunState.RESOLVED)
    result.final_patch = patch
    result.files_modified = ["sqlparse/sql.py"]
    result.summary = "Anchor on the last dot."
    result.test_runs.append(
        TestRunRecord(
            iteration=4, command="python -m pytest -q", passed=489, failed=0, errors=0,
            skipped=0, total=489, green=True, duration_ms=4000, framework="pytest",
            summary_recognized=True,
        )
    )
    await review_service.record_generated_patch(session, run, result)
    return run


# --- repository identity ---------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/andialbrecht/sqlparse", ("andialbrecht", "sqlparse")),
        ("https://github.com/andialbrecht/sqlparse.git", ("andialbrecht", "sqlparse")),
        ("git@github.com:more-itertools/more-itertools.git", ("more-itertools", "more-itertools")),
        ("ssh://git@github.com/owner/repo.git", ("owner", "repo")),
    ],
)
def test_repository_identity_is_parsed(url: str, expected: tuple[str, str]) -> None:
    identity = parse_repository(url)
    assert identity is not None
    assert (identity.owner, identity.repo) == expected


@pytest.mark.parametrize(
    "url",
    [None, "", "/local/path", "https://gitlab.com/owner/repo", "not a url",
     "https://github.com/onlyowner"],
)
def test_unrecognised_remotes_are_not_guessed(url) -> None:
    """A run we cannot identify is simply unpublishable, never guessed at."""
    assert parse_repository(url) is None


def test_a_clone_url_never_contains_credentials() -> None:
    identity = parse_repository("https://github.com/owner/repo")
    assert identity is not None
    assert "@" not in identity.clone_url()


# --- branch safety ---------------------------------------------------------


@pytest.mark.parametrize("protected", ["main", "master", "develop", "production", "MAIN"])
def test_protected_branches_are_refused(protected: str) -> None:
    with pytest.raises(ProtectedBranchError):
        ensure_pushable(protected)


def test_the_repositorys_own_default_branch_is_refused() -> None:
    with pytest.raises(ProtectedBranchError):
        ensure_pushable("patchpilot/whatever", default_branch="patchpilot/whatever")


def test_only_patchpilot_prefixed_branches_are_writable() -> None:
    with pytest.raises(ProtectedBranchError):
        ensure_pushable("feature/someone-elses-work")
    assert ensure_pushable("patchpilot/issue-332-fix") == "patchpilot/issue-332-fix"


def test_branch_names_are_deterministic_and_slugged() -> None:
    first = branch_name(run_id="abc123def", issue_number="332", title="get_real_name is wrong!")
    second = branch_name(run_id="abc123def", issue_number="332", title="get_real_name is wrong!")
    assert first == second, "a retry must compute the same name"
    assert first.startswith("patchpilot/issue-332")
    assert " " not in first and "!" not in first


def test_collisions_are_suffixed_never_reused() -> None:
    assert collision_name("patchpilot/x", 0) == "patchpilot/x"
    assert collision_name("patchpilot/x", 1) == "patchpilot/x-2"


# --- the publication workflow ----------------------------------------------


async def test_a_full_publication_creates_exactly_one_pull_request(session, db_settings) -> None:
    run = await _ready_run(session, db_settings)
    await review_service.approve(session, run, actor="alice")

    host = FakeHost()
    publication = await publish_service.PublicationService(host, db_settings).publish(
        session, run, actor="alice"
    )

    assert publication.pr_number == 842
    assert publication.branch.startswith("patchpilot/issue-332")
    assert publication.commit_sha == "c" * 40
    assert run.review_state == str(ReviewState.PR_CREATED)
    assert host.calls.count("create_pull_request") == 1
    assert host.calls.count("push_branch") == 1

    # The branch was cut from the reviewed base, and the approved bytes pushed.
    assert host.pushed[0]["base_commit_sha"] == BASE_SHA
    assert host.pushed[0]["patch"] == PATCH

    steps = publication.completed_steps
    assert str(PublicationStep.VERIFIED_DIGEST) in steps
    assert str(PublicationStep.VERIFIED_APPLIED_DIFF) in steps
    assert str(PublicationStep.PR_CREATED) in steps


async def test_publishing_twice_does_not_open_a_second_pull_request(
    session, db_settings
) -> None:
    """The double-clicked button."""
    run = await _ready_run(session, db_settings, run_id="pub-2")
    await review_service.approve(session, run, actor="alice")
    host = FakeHost()
    service = publish_service.PublicationService(host, db_settings)

    first = await service.publish(session, run, actor="alice")
    second = await service.publish(session, run, actor="alice")

    assert first.id == second.id
    assert first.pr_number == second.pr_number
    assert host.calls.count("create_pull_request") == 1
    assert host.calls.count("push_branch") == 1


async def test_publication_without_approval_is_refused(session, db_settings) -> None:
    """The invariant. PATCH_READY is not permission to publish."""
    run = await _ready_run(session, db_settings, run_id="pub-3")
    assert run.review_state == str(ReviewState.PATCH_READY)

    host = FakeHost()
    with pytest.raises(review_service.ReviewError, match="no patch has been approved"):
        await publish_service.PublicationService(host, db_settings).publish(session, run)
    assert host.calls == [], "nothing may touch the host without approval"


async def test_a_patch_altered_after_approval_is_not_published(session, db_settings) -> None:
    """approve A -> tamper -> publish must stop, not push B."""
    run = await _ready_run(session, db_settings, run_id="pub-4")
    version = await review_service.approve(session, run, actor="alice")

    version.diff = PATCH + "@@ -99 +99 @@\n+sneaky\n"
    await session.flush()

    host = FakeHost()
    with pytest.raises(review_service.ReviewError, match="does not match its approved digest"):
        await publish_service.PublicationService(host, db_settings).publish(session, run)
    assert host.pushed == []


async def test_base_drift_is_detected_and_the_approved_base_is_still_used(
    session, db_settings
) -> None:
    """main moved while the agent worked.

    The branch is still cut from the commit the patch was reviewed against, so
    the approved content is unchanged; the drift is recorded and surfaced.
    """
    run = await _ready_run(session, db_settings, run_id="pub-5")
    await review_service.approve(session, run, actor="alice")

    host = FakeHost(head=DRIFTED_SHA)
    publication = await publish_service.PublicationService(host, db_settings).publish(
        session, run, actor="alice"
    )

    assert publication.base_drifted is True
    assert publication.observed_base_head == DRIFTED_SHA
    assert publication.base_commit_sha == BASE_SHA
    assert host.pushed[0]["base_commit_sha"] == BASE_SHA, "never silently rebased"
    assert publication.pr_number is not None


async def test_an_unconfigured_host_fails_cleanly(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-6")
    await review_service.approve(session, run, actor="alice")

    host = FakeHost(configured=False)
    publication = await publish_service.PublicationService(host, db_settings).publish(
        session, run, actor="alice"
    )

    assert publication.error_code == str(PublishErrorCode.NOT_CONFIGURED)
    assert run.review_state == str(ReviewState.PUBLISH_FAILED)
    assert host.pushed == []


async def test_a_local_run_cannot_be_published(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-7")
    run.repo_owner = None
    run.repo_name = None
    run.repo_url = None
    await review_service.approve(session, run, actor="alice")

    publication = await publish_service.PublicationService(FakeHost(), db_settings).publish(
        session, run, actor="alice"
    )
    assert publication.error_code == str(PublishErrorCode.REPOSITORY_UNKNOWN)


async def test_a_failed_push_records_how_far_it_got(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-8")
    await review_service.approve(session, run, actor="alice")

    host = FakeHost(
        fail_push=GitHostingError(PublishErrorCode.PUSH_FAILED, "remote rejected the push")
    )
    publication = await publish_service.PublicationService(host, db_settings).publish(
        session, run, actor="alice"
    )

    assert publication.error_code == str(PublishErrorCode.PUSH_FAILED)
    assert run.review_state == str(ReviewState.PUBLISH_FAILED)
    # The steps before the failure are recorded so a retry can skip them.
    assert str(PublicationStep.CHECKED_DRIFT) in publication.completed_steps
    assert str(PublicationStep.BRANCH_PUSHED) not in publication.completed_steps


async def test_a_retry_after_a_lost_pr_response_adopts_the_existing_one(
    session, db_settings
) -> None:
    """branch pushed, PR call failed, retry: one PR, one push."""
    run = await _ready_run(session, db_settings, run_id="pub-9")
    await review_service.approve(session, run, actor="alice")

    host = FakeHost(
        fail_pr=GitHostingError(PublishErrorCode.PR_CREATE_FAILED, "gateway timeout")
    )
    service = publish_service.PublicationService(host, db_settings)
    failed = await service.publish(session, run, actor="alice")
    assert failed.error_code == str(PublishErrorCode.PR_CREATE_FAILED)
    assert str(PublicationStep.BRANCH_PUSHED) in failed.completed_steps
    branch = failed.branch

    # The PR did in fact get created on the far side; the response was lost.
    host.fail_pr = None
    host.pull_requests[branch] = PullRequestInfo(
        number=900, url="https://github.com/x/y/pull/900", state="open",
        head_branch=branch, base_branch="main",
    )
    retried = await service.publish(session, run, actor="alice")

    assert retried.pr_number == 900
    assert host.calls.count("push_branch") == 1, "the branch must not be pushed twice"
    # One attempt total: the first (which failed on the far side). The retry
    # found the existing pull request and adopted it rather than opening a
    # second one.
    assert host.calls.count("create_pull_request") == 1
    assert run.review_state == str(ReviewState.PR_CREATED)


async def test_a_branch_collision_gets_a_new_name_never_a_force_push(
    session, db_settings
) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-10")
    await review_service.approve(session, run, actor="alice")

    taken = branch_name(run_id=run.id, issue_number="332", title=run.issue_title)
    host = FakeHost(existing_branches={taken})
    publication = await publish_service.PublicationService(host, db_settings).publish(
        session, run, actor="alice"
    )

    assert publication.branch == collision_name(taken, 1)
    assert publication.branch != taken


async def test_the_audit_trail_covers_the_publication(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-11")
    await review_service.approve(session, run, actor="alice")
    await publish_service.PublicationService(FakeHost(), db_settings).publish(
        session, run, actor="alice"
    )

    actions = [a.action for a in await review_service.list_audit(session, run.id)]
    for expected in (
        ReviewAction.PATCH_GENERATED,
        ReviewAction.PATCH_APPROVED,
        ReviewAction.PUBLISH_STARTED,
        ReviewAction.BRANCH_CREATED,
        ReviewAction.COMMIT_CREATED,
        ReviewAction.BRANCH_PUSHED,
        ReviewAction.PR_CREATED,
    ):
        assert str(expected) in actions


# --- generated text --------------------------------------------------------


async def test_the_commit_message_needs_no_model_call(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-12")
    version = await review_service.latest_version(session, run.id)
    message = publish_service.commit_message(run, version)

    assert message.startswith("fix: resolve issue #332")
    assert "PatchPilot" in message
    assert "approved by a human" in message.lower()


async def test_the_pr_body_is_built_from_recorded_run_data(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-13")
    await review_service.approve(session, run, actor="alice")
    publication = await publish_service.PublicationService(FakeHost(), db_settings).publish(
        session, run, actor="alice"
    )
    version = await review_service.latest_version(session, run.id)
    body = publish_service.pull_request_body(run, version, publication)

    assert "## Summary" in body and "## Verification" in body
    assert "489 passed" in body
    assert "sqlparse/sql.py" in body
    assert "does not merge" in body
    # No closing keyword by default: opening a PR must not close someone's issue.
    assert "Closes #" not in body and "Fixes #" not in body


async def test_drift_is_explained_in_the_pull_request(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-14")
    await review_service.approve(session, run, actor="alice")
    publication = await publish_service.PublicationService(
        FakeHost(head=DRIFTED_SHA), db_settings
    ).publish(session, run, actor="alice")
    version = await review_service.latest_version(session, run.id)

    assert "moved while the agent was working" in publish_service.pull_request_body(
        run, version, publication
    )


async def test_status_refresh_reads_checks_and_never_merges(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="pub-15")
    await review_service.approve(session, run, actor="alice")
    host = FakeHost()
    service = publish_service.PublicationService(host, db_settings)
    publication = await service.publish(session, run, actor="alice")

    await service.refresh_status(session, publication)

    names = [c["name"] for c in publication.checks["runs"]]
    assert names == ["lint", "unit-tests"]
    assert publication.pr_state == "open"
    assert "merge" not in " ".join(host.calls)


# --- push failures must be actionable --------------------------------------


@pytest.mark.parametrize(
    ("git_error", "expected"),
    [
        (
            "git push failed: remote: Permission to owner/repo.git denied to user. "
            "fatal: The requested URL returned error: 403",
            PublishErrorCode.PERMISSION_DENIED,
        ),
        ("git push failed: remote: invalid credentials", PublishErrorCode.AUTH_FAILED),
        (
            "git push failed: remote: error: GH006: Protected branch update failed",
            PublishErrorCode.PROTECTED_BRANCH,
        ),
        (
            "git push failed: ! [rejected] main -> main (non-fast-forward)",
            PublishErrorCode.BRANCH_EXISTS,
        ),
        ("git push failed: could not resolve host", PublishErrorCode.PUSH_FAILED),
    ],
)
def test_push_failures_are_classified(git_error: str, expected: PublishErrorCode) -> None:
    """A 403 on push is a token permission, and should say so.

    The REST API cannot reveal this: on a public repository `GET /repos/{o}/{r}`
    reports the *owner's* permissions, so it happily says `push: true` for a
    token that cannot push at all. The push is the only honest signal.
    """
    from patchpilot.publish.github import _classify_push_failure

    classified = _classify_push_failure(GitHostingError(PublishErrorCode.UNKNOWN, git_error))
    assert classified.code is expected


def test_the_permission_message_names_the_setting_to_change() -> None:
    from patchpilot.publish.github import _classify_push_failure

    message = str(
        _classify_push_failure(
            GitHostingError(PublishErrorCode.UNKNOWN, "remote: Permission to x denied to y. 403")
        )
    )
    assert "Contents" in message and "Read and write" in message
    assert "selected repositories" in message


def test_a_token_never_appears_in_a_push_error() -> None:
    """Errors reach logs and the UI; a leaked credential there is permanent."""
    from patchpilot.publish.github import _classify_push_failure, _scrub

    leaked = "fatal: unable to access 'https://x-access-token:ghp_SECRETVALUE123@github.com/o/r'"
    assert "ghp_SECRETVALUE123" not in _scrub(leaked)
    classified = _classify_push_failure(GitHostingError(PublishErrorCode.UNKNOWN, leaked))
    assert "ghp_SECRETVALUE123" not in str(classified)
