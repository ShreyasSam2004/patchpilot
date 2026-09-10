"""Fork-based publication: contributing to a repository you cannot push to.

The direct path assumed the credential could write to upstream. For any project
you do not own it cannot, and the way every outside contribution has ever worked
is: fork it, push a branch to the fork, open a pull request from the fork
against upstream.

The property that must survive that extra hop is the one the whole review
workflow exists for -- **what lands is exactly what a human approved**. Two
design choices carry it, and both are pinned here:

* The approved base commit is fetched from *upstream*, not from the fork. A fork
  created after the review need not contain the reviewed commit, and syncing one
  could quietly move the patch onto a different revision.
* The pull request is opened against *upstream* with a ``user:branch`` head. A
  fork-to-fork pull request would be invisible to the maintainers it is for.

Nothing here touches GitHub. The recording double implements the same port the
adapter does, so ordering, idempotency and the refusal paths are exercised for
real rather than mocked at the HTTP layer.
"""

from __future__ import annotations

import pytest

from patchpilot.db.models import PublicationRow
from patchpilot.domain.enums import (
    PublicationStep,
    PublicationStrategy,
    PublishErrorCode,
    ReviewState,
)
from patchpilot.publish import service as publish_service
from patchpilot.publish.ports import GitHostingError
from patchpilot.publish.strategy import (
    PublicationTarget,
    resolve_fork,
    select_target,
    wait_until_ready,
)
from patchpilot.review import service as review_service
from tests.test_publication import BASE_SHA, FakeHost, _ready_run

UPSTREAM_OWNER = "external-owner"
UPSTREAM_REPO = "example-project"
USER = "test-user"


async def _external_run(session, settings, *, run_id: str = "fork-1"):
    """A run against a repository the credential does not own."""
    run = await _ready_run(session, settings, run_id=run_id)
    run.repository = f"{UPSTREAM_OWNER}/{UPSTREAM_REPO}"
    run.repo_url = f"https://github.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}"
    run.repo_owner = UPSTREAM_OWNER
    run.repo_name = UPSTREAM_REPO
    run.issue_url = f"https://github.com/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/issues/42"
    await session.flush()
    return run


async def _approve(session, run):
    return await review_service.approve(session, run, actor="reviewer")


def _service(settings, host: FakeHost):
    return publish_service.PublicationService(host, settings)


async def _publish(session, settings, host: FakeHost, run):
    return await _service(settings, host).publish(session, run, actor="reviewer")


# --- strategy selection ------------------------------------------------------


async def test_a_writable_upstream_selects_direct() -> None:
    host = FakeHost(can_push=True)
    target = await select_target(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO)

    assert target.strategy is PublicationStrategy.DIRECT
    assert (target.head_owner, target.head_repo) == (UPSTREAM_OWNER, UPSTREAM_REPO)
    assert "get_authenticated_user" not in host.calls, "no identity lookup is needed"
    assert host.forks_created == []


async def test_a_read_only_upstream_selects_fork() -> None:
    host = FakeHost(can_push=False, user=USER)
    target = await select_target(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO)

    assert target.strategy is PublicationStrategy.FORK
    assert (target.head_owner, target.head_repo) == (USER, UPSTREAM_REPO)
    assert target.upstream_owner == UPSTREAM_OWNER


async def test_unknown_permissions_stay_direct() -> None:
    """A GitHub App installation token reports no permissions block.

    Forking on every App publication would be wrong -- the App path is expected
    to have write access -- and a genuine refusal still surfaces on the push
    with nothing published.
    """
    host = FakeHost()
    host.can_push = True  # RepositoryInfo.can_push defaults True when absent

    target = await select_target(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO)
    assert target.strategy is PublicationStrategy.DIRECT


async def test_strategy_is_decided_before_anything_is_written() -> None:
    """Not by pushing and treating 403 as control flow.

    Failure-driven selection would make the normal path for every external
    repository an error, and would leave a half-created branch behind.
    """
    host = FakeHost(can_push=False, user=USER)
    await select_target(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO)

    assert "push_branch" not in host.calls
    assert "create_pull_request" not in host.calls


# --- fork discovery and creation ---------------------------------------------


async def test_an_existing_fork_is_reused() -> None:
    host = FakeHost(can_push=False, user=USER, existing_forks={f"{USER}/{UPSTREAM_REPO}"})
    fork = await resolve_fork(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO, user=USER)

    assert fork.full_name == f"{USER}/{UPSTREAM_REPO}"
    assert host.forks_created == [], "an existing fork must not be forked again"


async def test_a_missing_fork_is_created() -> None:
    host = FakeHost(can_push=False, user=USER)
    fork = await resolve_fork(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO, user=USER)

    assert fork.full_name == f"{USER}/{UPSTREAM_REPO}"
    assert host.forks_created == [f"{USER}/{UPSTREAM_REPO}"]


async def test_asynchronous_fork_creation_is_waited_out() -> None:
    """GitHub answers 202 and copies in the background; the repo 404s meanwhile."""
    host = FakeHost(can_push=False, user=USER, fork_ready_after=3)
    fork = await resolve_fork(
        host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO, user=USER, delay_s=0
    )

    assert fork.full_name == f"{USER}/{UPSTREAM_REPO}"
    assert host.calls.count("get_repository") >= 4, "it polled rather than assuming"


async def test_a_fork_that_never_appears_fails_within_bounds() -> None:
    """Bounded on purpose: hanging forever is worse than a clear failure."""
    host = FakeHost(can_push=False, user=USER, fork_ready_after=99)

    with pytest.raises(GitHostingError) as caught:
        await resolve_fork(
            host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO, user=USER,
            attempts=4, delay_s=0,
        )

    assert caught.value.code is PublishErrorCode.FORK_NOT_READY
    assert host.calls.count("get_repository") == 4, "exactly the attempts allowed"


async def test_a_failed_fork_creation_is_reported_as_such() -> None:
    host = FakeHost(
        can_push=False, user=USER,
        fail_fork=GitHostingError(PublishErrorCode.PERMISSION_DENIED, "no forking allowed"),
    )
    with pytest.raises(GitHostingError) as caught:
        await resolve_fork(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO, user=USER)

    assert caught.value.code is PublishErrorCode.FORK_CREATE_FAILED


async def test_a_same_named_repository_that_is_not_a_fork_is_not_adopted() -> None:
    """Publishing into an unrelated repository of theirs would be a surprise."""
    host = FakeHost(can_push=False, user=USER)
    # The user owns something called example-project, but it is not a fork.
    assert await host.find_fork(owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO, user=USER) is None


async def test_an_unidentifiable_credential_cannot_fork() -> None:
    host = FakeHost(can_push=False, user="")
    with pytest.raises(GitHostingError) as caught:
        await select_target(host, owner=UPSTREAM_OWNER, repo=UPSTREAM_REPO)
    assert caught.value.code is PublishErrorCode.IDENTITY_UNKNOWN


async def test_waiting_returns_as_soon_as_the_fork_answers() -> None:
    host = FakeHost(existing_forks={f"{USER}/{UPSTREAM_REPO}"})
    info = await wait_until_ready(host, owner=USER, repo=UPSTREAM_REPO, attempts=5, delay_s=0)
    assert info.full_name == f"{USER}/{UPSTREAM_REPO}"
    assert host.calls.count("get_repository") == 1


# --- the realistic end-to-end scenario ---------------------------------------


async def test_the_full_fork_publication(session, db_settings) -> None:
    """external-owner/example-project, pushed from test-user's fork.

        fork      = test-user/example-project
        branch    = patchpilot/issue-42-...
        PR target = external-owner/example-project:main
        PR head   = test-user:patchpilot/issue-42-...
    """
    run = await _external_run(session, db_settings)
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    publication = await _publish(session, db_settings, host, run)

    assert publication.strategy == str(PublicationStrategy.FORK)
    assert (publication.head_owner, publication.head_repo) == (USER, UPSTREAM_REPO)
    assert (publication.owner, publication.repo) == (UPSTREAM_OWNER, UPSTREAM_REPO)

    # The branch went to the fork...
    assert len(host.pushed) == 1
    push = host.pushed[0]
    assert push["owner"] == USER and push["repo"] == UPSTREAM_REPO
    assert push["branch"].startswith("patchpilot/")
    # ...but the base commit was read from upstream.
    assert push["source_owner"] == UPSTREAM_OWNER
    assert push["source_repo"] == UPSTREAM_REPO
    assert push["base_commit_sha"] == BASE_SHA

    # And the pull request targets upstream with a qualified head.
    pr = host.pull_requests[f"{USER}:{publication.branch}"]
    assert pr.head_branch == f"{USER}:{publication.branch}"
    assert pr.base_branch == "main"
    assert run.review_state == str(ReviewState.PR_CREATED)
    assert publication.pr_number == pr.number


async def test_a_fork_pull_request_is_never_opened_against_the_fork(
    session, db_settings
) -> None:
    """user/project -> user/project would be invisible to the maintainers."""
    run = await _external_run(session, db_settings, run_id="fork-2")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    await _publish(session, db_settings, host, run)

    created = [c for c in host.calls if c == "create_pull_request"]
    assert len(created) == 1
    for pr in host.pull_requests.values():
        assert not pr.head_branch.startswith(f"{UPSTREAM_OWNER}:")
        assert pr.base_branch == "main"


async def test_direct_publication_head_stays_unqualified(session, db_settings) -> None:
    """The existing behaviour must be byte-identical, not merely equivalent."""
    run = await _ready_run(session, db_settings, run_id="direct-1")
    await _approve(session, run)
    host = FakeHost(can_push=True)

    publication = await _publish(session, db_settings, host, run)

    assert publication.strategy == str(PublicationStrategy.DIRECT)
    assert publication.head_owner == "andialbrecht"
    pr = host.pull_requests[str(publication.branch)]
    assert ":" not in pr.head_branch, "direct heads are plain branch names"
    assert host.forks_created == []


# --- patch integrity across the extra hop ------------------------------------


async def test_the_approved_digest_survives_fork_publication(session, db_settings) -> None:
    run = await _external_run(session, db_settings, run_id="fork-3")
    version = await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    publication = await _publish(session, db_settings, host, run)

    assert publication.patch_digest == run.approved_patch_sha256
    assert host.pushed[0]["patch"] == version.diff, "the approved bytes, unchanged"
    assert publication.has_completed(str(PublicationStep.VERIFIED_DIGEST))
    assert publication.has_completed(str(PublicationStep.VERIFIED_APPLIED_DIFF))


async def test_a_patch_altered_after_approval_is_not_forked_either(
    session, db_settings
) -> None:
    """The digest gate is not weakened for fork mode.

    It bites even earlier than the publication step machine: loading the
    approved version re-checks the digest, so a tampered patch never reaches
    strategy selection. Nothing is forked and nothing is pushed.
    """
    from patchpilot.review.service import ReviewError

    run = await _external_run(session, db_settings, run_id="fork-4")
    version = await _approve(session, run)
    version.diff = version.diff + "\n+ sneaked in\n"
    await session.flush()

    host = FakeHost(can_push=False, user=USER)
    with pytest.raises(ReviewError, match="does not match its approved digest"):
        await _publish(session, db_settings, host, run)

    assert host.pushed == [], "nothing was pushed"
    assert host.forks_created == [], "and no fork was made on the way to failing"


async def test_a_mismatched_applied_diff_aborts_before_push(session, db_settings) -> None:
    """The adapter re-derives the diff and refuses; the service records it."""
    run = await _external_run(session, db_settings, run_id="fork-5")
    await _approve(session, run)
    host = FakeHost(
        can_push=False, user=USER,
        fail_push=GitHostingError(
            PublishErrorCode.APPLIED_DIFF_MISMATCH, "diff differs from the approved patch"
        ),
    )

    publication = await _publish(session, db_settings, host, run)

    assert publication.error_code == str(PublishErrorCode.APPLIED_DIFF_MISMATCH)
    assert publication.pr_number is None
    assert "create_pull_request" not in host.calls


async def test_an_unavailable_base_commit_stops_the_publication(
    session, db_settings
) -> None:
    """A reviewed commit that upstream no longer has cannot be published safely.

    Rebasing onto a newer commit would change what was approved, so this stops
    and asks for a fresh review instead.
    """
    run = await _external_run(session, db_settings, run_id="fork-6")
    await _approve(session, run)
    host = FakeHost(
        can_push=False, user=USER,
        fail_push=GitHostingError(
            PublishErrorCode.FORK_BASE_UNAVAILABLE, "base commit could not be fetched"
        ),
    )

    publication = await _publish(session, db_settings, host, run)

    assert publication.error_code == str(PublishErrorCode.FORK_BASE_UNAVAILABLE)
    assert publication.pr_number is None


async def test_base_drift_is_recorded_and_the_approved_base_still_used(
    session, db_settings
) -> None:
    run = await _external_run(session, db_settings, run_id="fork-7")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER, head="f" * 40)

    publication = await _publish(session, db_settings, host, run)

    assert publication.base_drifted is True
    assert publication.observed_base_head == "f" * 40
    assert host.pushed[0]["base_commit_sha"] == BASE_SHA, "still the approved base"


# --- branch safety ------------------------------------------------------------


async def test_the_fork_branch_uses_the_patchpilot_namespace(
    session, db_settings
) -> None:
    run = await _external_run(session, db_settings, run_id="fork-8")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    publication = await _publish(session, db_settings, host, run)
    assert str(publication.branch).startswith("patchpilot/")


async def test_neither_default_branch_can_be_written(session, db_settings) -> None:
    """Upstream's default and the fork's own default are both refused.

    A fork can have a different default branch from its parent, so guarding
    only against upstream's name would leave the fork's own default writable.
    """
    from patchpilot.publish.branch import ProtectedBranchError, ensure_pushable

    for protected in ("main", "master", "develop", "production"):
        with pytest.raises(ProtectedBranchError):
            ensure_pushable(protected)

    with pytest.raises(ProtectedBranchError):
        ensure_pushable("their-default", default_branch="their-default")

    run = await _external_run(session, db_settings, run_id="fork-9")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER, fork_default_branch="trunk-of-fork")
    publication = await _publish(session, db_settings, host, run)

    assert str(publication.branch) not in {"main", "trunk-of-fork"}
    assert host.pushed[0]["protected_default_branch"] == "trunk-of-fork"


# --- idempotency and retry ----------------------------------------------------


async def test_publishing_twice_makes_one_fork_and_one_pull_request(
    session, db_settings
) -> None:
    run = await _external_run(session, db_settings, run_id="fork-10")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    first = await _publish(session, db_settings, host, run)
    second = await _publish(session, db_settings, host, run)

    assert first.id == second.id
    assert host.forks_created == [f"{USER}/{UPSTREAM_REPO}"], "one fork only"
    assert len(host.pushed) == 1, "no duplicate commit"
    assert host.calls.count("create_pull_request") == 1
    assert first.pr_number == second.pr_number


async def test_a_retry_after_fork_creation_does_not_fork_again(
    session, db_settings
) -> None:
    """The failure lands after the fork exists; the retry must adopt it."""
    run = await _external_run(session, db_settings, run_id="fork-11")
    await _approve(session, run)
    host = FakeHost(
        can_push=False, user=USER,
        fail_push=GitHostingError(PublishErrorCode.RATE_LIMITED, "slow down"),
    )

    failed = await _publish(session, db_settings, host, run)
    assert failed.error_code == str(PublishErrorCode.RATE_LIMITED)
    assert host.forks_created == [f"{USER}/{UPSTREAM_REPO}"]
    assert failed.head_owner == USER

    host.fail_push = None
    retried = await _publish(session, db_settings, host, run)

    assert retried.pr_number is not None
    assert host.forks_created == [f"{USER}/{UPSTREAM_REPO}"], "still just the one"
    assert host.calls.count("create_fork") == 1


async def test_a_retry_after_push_adopts_the_existing_pull_request(
    session, db_settings
) -> None:
    """Crash between push and recording the PR: the branch and PR both exist."""
    run = await _external_run(session, db_settings, run_id="fork-12")
    await _approve(session, run)
    host = FakeHost(
        can_push=False, user=USER,
        fail_pr=GitHostingError(PublishErrorCode.RATE_LIMITED, "slow down"),
    )

    failed = await _publish(session, db_settings, host, run)
    assert failed.has_completed(str(PublicationStep.BRANCH_PUSHED))
    assert failed.pr_number is None
    pushes_before = len(host.pushed)

    host.fail_pr = None
    retried = await _publish(session, db_settings, host, run)

    assert retried.pr_number is not None
    assert len(host.pushed) == pushes_before, "the commit was not recreated"


async def test_an_existing_pull_request_is_adopted_not_duplicated(
    session, db_settings
) -> None:
    """GitHub opened the pull request but PatchPilot never saw the response.

    Simulated by discarding everything that final step recorded -- the number,
    the URL, the step marker, and the run's terminal state -- which is what a
    process killed mid-write leaves behind. The retry must find the pull
    request that already exists instead of opening a second one.
    """
    run = await _external_run(session, db_settings, run_id="fork-13")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    first = await _publish(session, db_settings, host, run)
    branch = str(first.branch)
    first.pr_number = None
    first.pr_url = None
    first.completed_steps = [
        step for step in first.completed_steps if step != str(PublicationStep.PR_CREATED)
    ]
    first.state = str(ReviewState.PUBLISHING)
    run.review_state = str(ReviewState.APPROVED)
    await session.flush()

    again = await _publish(session, db_settings, host, run)

    assert host.calls.count("create_pull_request") == 1, "adopted, not recreated"
    assert again.pr_number is not None
    assert again.branch == branch, "and the same branch was reused"


async def test_the_recorded_steps_describe_how_far_a_fork_publication_got(
    session, db_settings
) -> None:
    run = await _external_run(session, db_settings, run_id="fork-14")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    publication = await _publish(session, db_settings, host, run)

    for step in (
        PublicationStep.VERIFIED_DIGEST,
        PublicationStep.RESOLVED_REPOSITORY,
        PublicationStep.SELECTED_STRATEGY,
        PublicationStep.RESOLVED_FORK,
        PublicationStep.CHECKED_DRIFT,
        PublicationStep.COMMIT_CREATED,
        PublicationStep.BRANCH_PUSHED,
        PublicationStep.PR_CREATED,
    ):
        assert publication.has_completed(str(step)), f"{step} was not recorded"


async def test_direct_publication_records_no_fork_step(session, db_settings) -> None:
    run = await _ready_run(session, db_settings, run_id="direct-2")
    await _approve(session, run)
    host = FakeHost(can_push=True)

    publication = await _publish(session, db_settings, host, run)
    assert not publication.has_completed(str(PublicationStep.RESOLVED_FORK))
    assert publication.has_completed(str(PublicationStep.SELECTED_STRATEGY))


# --- security -----------------------------------------------------------------


async def test_no_credential_reaches_the_publication_record(
    session, db_settings
) -> None:
    """The row is serialised to the API and the UI; a token in it is permanent."""
    db_settings.github_token = "ghp_SECRETVALUE1234567890"
    run = await _external_run(session, db_settings, run_id="fork-15")
    await _approve(session, run)
    host = FakeHost(can_push=False, user=USER)

    publication = await _publish(session, db_settings, host, run)

    serialised = " ".join(
        str(getattr(publication, column.name))
        for column in PublicationRow.__table__.columns
    )
    assert "ghp_SECRET" not in serialised
    assert "@github.com" not in serialised, "no authenticated remote URL is stored"


def test_the_port_still_offers_no_way_to_merge() -> None:
    """Fork support adds reach, not authority."""
    from patchpilot.publish.ports import GitHostingPort

    forbidden = {
        "merge_pull_request", "merge", "auto_merge", "enable_auto_merge",
        "approve_review", "submit_review", "dismiss_review",
        "update_branch_protection", "delete_branch_protection",
        "update_repository", "delete_repository", "request", "raw",
    }
    assert forbidden.isdisjoint(dir(GitHostingPort))


def test_the_adapter_exposes_no_generic_escape_hatch() -> None:
    """A generic endpoint would put every operation back within reach."""
    from patchpilot.publish.github import GitHubAdapter

    public = {name for name in dir(GitHubAdapter) if not name.startswith("_")}
    assert "merge_pull_request" not in public
    assert not {"execute", "run_git", "api", "call"} & public


# --- the target value object --------------------------------------------------


def test_the_head_ref_is_qualified_only_for_forks() -> None:
    direct = PublicationTarget(
        strategy=PublicationStrategy.DIRECT,
        upstream_owner="o", upstream_repo="r", upstream_default_branch="main",
        head_owner="o", head_repo="r", head_default_branch="main",
    )
    fork = PublicationTarget(
        strategy=PublicationStrategy.FORK,
        upstream_owner="o", upstream_repo="r", upstream_default_branch="main",
        head_owner="u", head_repo="r", head_default_branch="main",
    )
    assert direct.head_ref("patchpilot/x") == "patchpilot/x"
    assert fork.head_ref("patchpilot/x") == "u:patchpilot/x"
    assert "fork publication" in fork.describe()
    assert "direct publication" in direct.describe()


# --- abbreviated base commits -------------------------------------------------


def test_an_abbreviation_is_not_mistaken_for_drift() -> None:
    """`86112e6` and its full SHA are the same commit.

    A plain inequality called every such publication drifted while pointing at
    the very commit it was approved against.
    """
    from patchpilot.publish.service import _has_drifted

    full = "86112e6" + "a" * 33
    assert _has_drifted(full, "86112e6") is False
    assert _has_drifted(full, full) is False
    assert _has_drifted(full, "268615a") is True
    assert _has_drifted(full, None) is False
    assert _has_drifted(None, full) is False


async def test_an_abbreviated_base_commit_is_expanded_before_use(
    session, db_settings
) -> None:
    """`git fetch <remote> 86112e6` fails: the protocol wants all 40 characters.

    The run recorded what the caller typed, so publication expands it. Resolving
    is not a change of revision -- both forms name the same commit -- and the
    push must receive the full SHA.
    """
    run = await _external_run(session, db_settings, run_id="fork-16")
    run.base_commit = "86112e6"
    await session.flush()
    await _approve(session, run)

    host = FakeHost(can_push=False, user=USER, head="86112e6" + "0" * 33)
    publication = await _publish(session, db_settings, host, run)

    assert "resolve_commit" in host.calls
    assert len(publication.base_commit_sha) == 40
    assert publication.base_commit_sha.startswith("86112e6")
    assert len(host.pushed[0]["base_commit_sha"]) == 40
    assert publication.base_drifted is False, "the same commit is not drift"
    assert publication.pr_number is not None


async def test_a_full_base_commit_is_left_alone(session, db_settings) -> None:
    """No extra request when there is nothing to resolve."""
    run = await _external_run(session, db_settings, run_id="fork-17")
    await _approve(session, run)

    host = FakeHost(can_push=False, user=USER)
    publication = await _publish(session, db_settings, host, run)

    assert "resolve_commit" not in host.calls
    assert publication.base_commit_sha == BASE_SHA


async def test_a_base_commit_that_no_longer_exists_stops_publication(
    session, db_settings
) -> None:
    """Force-pushed away upstream. Guessing a replacement would change what was
    approved, so this stops and says so."""
    run = await _external_run(session, db_settings, run_id="fork-18")
    run.base_commit = "deadbee"
    await session.flush()
    await _approve(session, run)

    host = FakeHost(can_push=False, user=USER)
    host.unresolvable_refs = {"deadbee"}
    publication = await _publish(session, db_settings, host, run)

    assert publication.error_code == str(PublishErrorCode.BASE_COMMIT_UNKNOWN)
    assert host.pushed == []
    assert publication.pr_number is None
