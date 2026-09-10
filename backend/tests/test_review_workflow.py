"""Human review: state machine, patch versioning, and approval integrity.

The invariant these tests exist to protect is narrow and absolute: *what gets
published is the exact thing a human approved*. Everything else here -- the
transition table, the version history, the audit trail -- is in service of
being able to prove that after the fact.
"""

from __future__ import annotations

import pytest

from patchpilot.agent.result import AgentResult, HypothesisRecord, TestRunRecord
from patchpilot.db.models import Run
from patchpilot.domain.enums import ReviewAction, ReviewState, RunState
from patchpilot.review import service as review_service
from patchpilot.review.digest import canonical_patch, patch_digest
from patchpilot.review.states import InvalidReviewTransition, can_transition, ensure_transition

REAL_FIX = (
    "diff --git a/sqlparse/sql.py b/sqlparse/sql.py\n"
    "--- a/sqlparse/sql.py\n"
    "+++ b/sqlparse/sql.py\n"
    "@@ -18,8 +18,9 @@\n"
    "-        dot_idx, _ = self.token_next_by(m=(T.Punctuation, '.'))\n"
    "+        dot_idx = self._last_dot_index()\n"
)
REVISED_FIX = REAL_FIX + "@@ -40,2 +41,3 @@\n+    # also handle the sibling case\n"


def make_run(**overrides) -> Run:
    defaults: dict[str, object] = {
        "id": "run-review-1",
        "code": "R001",
        "repository": "andialbrecht/sqlparse",
        "repo_url": "https://github.com/andialbrecht/sqlparse",
        "repo_provider": "github",
        "repo_owner": "andialbrecht",
        "repo_name": "sqlparse",
        "base_commit": "df8e284" + "0" * 33,
        "issue": "get_real_name returns the wrong component",
        "issue_title": "get_real_name returns the wrong component",
        "issue_url": "https://github.com/andialbrecht/sqlparse/issues/332",
        "model": "claude-opus-5",
        "state": str(RunState.RESOLVED),
        "review_state": str(ReviewState.RUNNING),
    }
    defaults.update(overrides)
    return Run(**defaults)


def make_result(patch: str, *, green: bool = True, recognized: bool = True) -> AgentResult:
    result = AgentResult(run_id="run-review-1", state=RunState.RESOLVED)
    result.final_patch = patch
    result.files_modified = ["sqlparse/sql.py"]
    result.summary = "Anchor on the last dot instead of the first."
    result.metrics.iterations = 6
    result.metrics.verified_by_tests = green and recognized
    result.hypotheses.append(
        HypothesisRecord(
            iteration=3,
            index=1,
            suspected_cause="token_next_by finds the first dot, not the last",
            evidence="sqlparse/sql.py:19",
            files_involved=["sqlparse/sql.py"],
            proposed_fix="anchor on the last dot",
            validation_plan="python -m pytest -q",
        )
    )
    result.test_runs.append(
        TestRunRecord(
            iteration=5,
            command="python -m pytest -q",
            passed=489,
            failed=0,
            errors=0,
            skipped=0,
            total=489,
            green=green,
            duration_ms=4080,
            framework="pytest",
            summary_recognized=recognized,
        )
    )
    return result


# --- the transition table ------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (ReviewState.RUNNING, ReviewState.PATCH_READY),
        (ReviewState.PATCH_READY, ReviewState.APPROVED),
        (ReviewState.PATCH_READY, ReviewState.REVISION_REQUESTED),
        (ReviewState.PATCH_READY, ReviewState.REJECTED),
        (ReviewState.REVISION_REQUESTED, ReviewState.REVISING),
        (ReviewState.REVISING, ReviewState.PATCH_READY),
        (ReviewState.APPROVED, ReviewState.PUBLISHING),
        (ReviewState.PUBLISHING, ReviewState.PR_CREATED),
        (ReviewState.PUBLISHING, ReviewState.PUBLISH_FAILED),
        (ReviewState.PUBLISH_FAILED, ReviewState.PUBLISHING),
    ],
)
def test_the_documented_transitions_are_allowed(current, target) -> None:
    assert ensure_transition(current, target) is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        # The one that matters: publication without approval.
        (ReviewState.PATCH_READY, ReviewState.PUBLISHING),
        (ReviewState.PATCH_READY, ReviewState.PR_CREATED),
        (ReviewState.RUNNING, ReviewState.APPROVED),
        (ReviewState.REJECTED, ReviewState.APPROVED),
        (ReviewState.PR_CREATED, ReviewState.PUBLISHING),
        (ReviewState.REVISION_REQUESTED, ReviewState.APPROVED),
    ],
)
def test_invalid_transitions_are_refused(current, target) -> None:
    assert not can_transition(current, target)
    with pytest.raises(InvalidReviewTransition):
        ensure_transition(current, target)


def test_a_terminal_state_goes_nowhere() -> None:
    for terminal in (ReviewState.PR_CREATED, ReviewState.REJECTED):
        assert terminal.is_terminal
        for target in ReviewState:
            if target is not terminal:
                assert not can_transition(terminal, target)


def test_a_repeated_decision_is_a_no_op_not_an_error() -> None:
    """Double-clicked buttons must not raise."""
    assert ensure_transition(ReviewState.APPROVED, ReviewState.APPROVED) is ReviewState.APPROVED


# --- digests bind approval to bytes --------------------------------------


def test_a_changed_patch_produces_a_different_digest() -> None:
    assert patch_digest(REAL_FIX) != patch_digest(REVISED_FIX)


def test_line_endings_do_not_change_the_digest() -> None:
    """A diff round-tripping through a database column must still match."""
    assert patch_digest(REAL_FIX) == patch_digest(REAL_FIX.replace("\n", "\r\n"))


def test_a_single_added_blank_line_does_change_the_digest() -> None:
    """Canonicalisation must not be so eager that it hides a real edit."""
    tampered = REAL_FIX.replace("@@ -18,8 +18,9 @@\n", "@@ -18,8 +18,9 @@\n \n")
    assert patch_digest(tampered) != patch_digest(REAL_FIX)


def test_an_empty_patch_never_matches_anything() -> None:
    assert patch_digest("") == ""
    assert patch_digest("   \n") == ""
    assert canonical_patch("  \n") == ""


# --- versioning and decisions (against a real session) --------------------


async def test_a_generated_patch_becomes_version_one(session) -> None:
    run = make_run()
    session.add(run)
    await session.flush()

    version = await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    assert version is not None
    assert version.version == 1
    assert version.digest == patch_digest(REAL_FIX)
    assert run.review_state == str(ReviewState.PATCH_READY)
    assert version.verification["passed"] == 489
    assert version.verification["summary_recognized"] is True
    assert version.root_cause.startswith("token_next_by")


async def test_a_patch_that_is_only_debris_never_reaches_a_reviewer(session) -> None:
    """Nobody should be asked to review a debug script."""
    run = make_run(id="run-review-debris", code="R002")
    session.add(run)
    await session.flush()

    scratch = (
        "diff --git a/repro.py b/repro.py\n--- /dev/null\n+++ b/repro.py\n@@\n+print(1)\n"
    )
    assert await review_service.record_generated_patch(session, run, make_result(scratch)) is None
    assert run.review_state == str(ReviewState.RUNNING)


async def test_a_revision_adds_a_version_and_keeps_the_first(session) -> None:
    run = make_run(id="run-review-2", code="R003")
    session.add(run)
    await session.flush()

    v1 = await review_service.record_generated_patch(session, run, make_result(REAL_FIX))
    await review_service.request_revision(
        session, run, feedback="Also check the sibling method.", actor="alice"
    )
    assert run.review_state == str(ReviewState.REVISION_REQUESTED)
    assert v1.reviewer_feedback == "Also check the sibling method."
    assert run.config["reviewer_feedback"] == "Also check the sibling method."

    await review_service.begin_revision(session, run)
    v2 = await review_service.record_generated_patch(session, run, make_result(REVISED_FIX))

    assert v2.version == 2
    assert run.review_state == str(ReviewState.PATCH_READY)
    versions = await review_service.list_versions(session, run.id)
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].diff == REAL_FIX, "V1 must survive verbatim"
    assert versions[0].decision == str(ReviewAction.REVISION_REQUESTED)


async def test_revision_requires_feedback(session) -> None:
    run = make_run(id="run-review-3", code="R004")
    session.add(run)
    await session.flush()
    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    with pytest.raises(review_service.ReviewError):
        await review_service.request_revision(session, run, feedback="   ")


async def test_approval_records_the_digest_and_the_reviewer(session) -> None:
    run = make_run(id="run-review-4", code="R005")
    session.add(run)
    await session.flush()
    version = await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    approved = await review_service.approve(session, run, actor="alice@example.com")

    assert approved.version == version.version
    assert run.review_state == str(ReviewState.APPROVED)
    assert run.approved_patch_sha256 == patch_digest(REAL_FIX)
    assert run.approved_by == "alice@example.com"
    assert run.approved_at is not None


async def test_approving_twice_is_idempotent(session) -> None:
    run = make_run(id="run-review-5", code="R006")
    session.add(run)
    await session.flush()
    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    first = await review_service.approve(session, run, actor="alice")
    second = await review_service.approve(session, run, actor="alice")
    assert first.version == second.version
    assert run.review_state == str(ReviewState.APPROVED)


async def test_a_stale_digest_refuses_the_approval(session) -> None:
    """The reviewer clicked Approve on a page showing an older patch."""
    run = make_run(id="run-review-6", code="R007")
    session.add(run)
    await session.flush()
    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    with pytest.raises(review_service.ReviewError, match="changed since it was displayed"):
        await review_service.approve(session, run, expected_digest=patch_digest(REVISED_FIX))
    assert run.review_state == str(ReviewState.PATCH_READY)
    assert run.approved_patch_sha256 is None


async def test_a_new_patch_invalidates_an_existing_approval(session) -> None:
    """Approve V1, agent produces V2 -> V1's approval must not survive.

    This is the "approve patch A, publish patch B" hole. Without invalidation a
    later publication would push something no human ever saw.
    """
    run = make_run(id="run-review-7", code="R008")
    session.add(run)
    await session.flush()
    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))
    await review_service.approve(session, run, actor="alice")
    assert run.approved_patch_sha256 == patch_digest(REAL_FIX)

    # A re-run lands a different patch on the same run.
    run.review_state = str(ReviewState.RUNNING)
    await review_service.record_generated_patch(session, run, make_result(REVISED_FIX))

    assert run.approved_patch_sha256 is None, "the old approval must be dropped"
    assert run.review_state == str(ReviewState.PATCH_READY)
    actions = [a.action for a in await review_service.list_audit(session, run.id)]
    assert str(ReviewAction.APPROVAL_INVALIDATED) in actions


async def test_an_identical_regenerated_patch_keeps_its_approval(session) -> None:
    """A re-run that produces the same bytes is not a change."""
    run = make_run(id="run-review-8", code="R009")
    session.add(run)
    await session.flush()
    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))
    await review_service.approve(session, run, actor="alice")

    run.review_state = str(ReviewState.RUNNING)
    again = await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    assert again.version == 1, "no spurious V2 for identical content"
    assert run.approved_patch_sha256 == patch_digest(REAL_FIX)


async def test_rejection_is_terminal_and_clears_approval(session) -> None:
    run = make_run(id="run-review-9", code="R010")
    session.add(run)
    await session.flush()
    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))

    await review_service.reject(session, run, feedback="Wrong approach.", actor="bob")

    assert run.review_state == str(ReviewState.REJECTED)
    assert run.approved_patch_sha256 is None
    with pytest.raises(InvalidReviewTransition):
        await review_service.approve(session, run)


async def test_approved_version_refuses_a_tampered_patch(session) -> None:
    """The stored diff is re-hashed before publication may use it."""
    run = make_run(id="run-review-10", code="R011")
    session.add(run)
    await session.flush()
    version = await review_service.record_generated_patch(session, run, make_result(REAL_FIX))
    await review_service.approve(session, run, actor="alice")

    version.diff = REVISED_FIX  # tampered after approval, digest column unchanged
    with pytest.raises(review_service.ReviewError, match="does not match its approved digest"):
        await review_service.approved_version(session, run)


async def test_the_audit_trail_records_every_decision(session) -> None:
    run = make_run(id="run-review-11", code="R012")
    session.add(run)
    await session.flush()

    await review_service.record_generated_patch(session, run, make_result(REAL_FIX))
    await review_service.request_revision(session, run, feedback="More.", actor="alice")
    await review_service.begin_revision(session, run)
    await review_service.record_generated_patch(session, run, make_result(REVISED_FIX))
    await review_service.approve(session, run, actor="bob")

    actions = [a.action for a in await review_service.list_audit(session, run.id)]
    assert actions == [
        str(ReviewAction.PATCH_GENERATED),
        str(ReviewAction.REVISION_REQUESTED),
        str(ReviewAction.PATCH_REVISED),
        str(ReviewAction.PATCH_APPROVED),
    ]
    audit = await review_service.list_audit(session, run.id)
    assert audit[1].feedback == "More."
    assert audit[1].actor == "alice"
    assert audit[-1].patch_digest == patch_digest(REVISED_FIX)


# --- the base commit publication depends on -------------------------------


async def test_the_prepared_revision_is_recorded_on_the_run(session) -> None:
    """A URL run without a pinned commit must still record what it prepared.

    Publication applies the approved patch to a known revision. Without this the
    run reaches PATCH_READY, the reviewer can approve, and publication then has
    nowhere to apply the patch — which is exactly what happened to the first
    real integration run.
    """
    from patchpilot.runs import service as run_service

    run = make_run(id="run-base-1", code="R100", base_commit=None)
    run.base_branch = None
    session.add(run)
    await session.flush()

    result = make_result(REAL_FIX)
    result.base_commit = "e" * 40
    result.base_branch = "main"
    await run_service.apply_result(session, run.id, result)

    assert run.base_commit == "e" * 40
    assert run.base_branch == "main"


async def test_an_explicitly_pinned_commit_is_never_overwritten(session) -> None:
    """A caller who pinned a base commit meant it."""
    from patchpilot.runs import service as run_service

    pinned = "d" * 40
    run = make_run(id="run-base-2", code="R101", base_commit=pinned)
    session.add(run)
    await session.flush()

    result = make_result(REAL_FIX)
    result.base_commit = "f" * 40  # what the workspace happened to resolve
    await run_service.apply_result(session, run.id, result)

    assert run.base_commit == pinned
