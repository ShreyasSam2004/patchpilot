"""Enumerations shared by the agent, persistence, API and UI.

These string values are part of the API contract — the frontend switches on
them directly — so they are stable and lower-snake/upper-snake by convention.
"""

from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    """Explicit agent state machine.

    Terminal states are :meth:`is_terminal`. Everything else is a working state
    the orchestrator moves through as it selects tools.
    """

    PENDING = "PENDING"
    PREPARING = "PREPARING"
    ANALYZING = "ANALYZING"
    SEARCHING = "SEARCHING"
    READING = "READING"
    HYPOTHESIZING = "HYPOTHESIZING"
    PATCHING = "PATCHING"
    TESTING = "TESTING"
    REFLECTING = "REFLECTING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return not self.is_terminal


_TERMINAL_STATES = frozenset(
    {RunState.RESOLVED, RunState.FAILED, RunState.TIMEOUT, RunState.CANCELLED}
)


class EventType(StrEnum):
    """Structured, user-visible run events.

    Model chain-of-thought is never stored. Only tool calls, their results,
    explicit hypotheses the agent recorded, and lifecycle transitions.
    """

    RUN_STARTED = "RUN_STARTED"
    REPOSITORY_READY = "REPOSITORY_READY"
    ISSUE_ANALYZED = "ISSUE_ANALYZED"
    STATE_CHANGED = "STATE_CHANGED"
    ITERATION_STARTED = "ITERATION_STARTED"
    MODEL_CALL_STARTED = "MODEL_CALL_STARTED"
    MODEL_CALL_FINISHED = "MODEL_CALL_FINISHED"
    AGENT_MESSAGE = "AGENT_MESSAGE"
    TOOL_CALLED = "TOOL_CALLED"
    TOOL_RESULT = "TOOL_RESULT"
    HYPOTHESIS_CREATED = "HYPOTHESIS_CREATED"
    PATCH_APPLIED = "PATCH_APPLIED"
    TEST_STARTED = "TEST_STARTED"
    TEST_FINISHED = "TEST_FINISHED"
    ITERATION_COMPLETED = "ITERATION_COMPLETED"
    LOOP_DETECTED = "LOOP_DETECTED"
    RECOVERY_TRIGGERED = "RECOVERY_TRIGGERED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    WARNING = "WARNING"
    RUN_RESOLVED = "RUN_RESOLVED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"

    # -- human review and publication ------------------------------------
    PATCH_READY = "PATCH_READY"
    PATCH_REVISION_REQUESTED = "PATCH_REVISION_REQUESTED"
    PATCH_APPROVED = "PATCH_APPROVED"
    PATCH_REJECTED = "PATCH_REJECTED"
    PUBLICATION_STARTED = "PUBLICATION_STARTED"
    GITHUB_BRANCH_CREATED = "GITHUB_BRANCH_CREATED"
    GITHUB_PULL_REQUEST_CREATED = "GITHUB_PULL_REQUEST_CREATED"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"


class ReviewState(StrEnum):
    """Where a run sits in the human review and publication workflow.

    Deliberately separate from :class:`RunState`. That one is what the *agent*
    is doing; this is what a *reviewer* has decided. Collapsing them would make
    "the agent believes it is done" indistinguishable from "a human approved
    this exact patch for publication", which is the one distinction this whole
    workflow exists to preserve.
    """

    RUNNING = "RUNNING"
    """The agent is still working; there is nothing to review yet."""
    PATCH_READY = "PATCH_READY"
    """A meaningful patch exists and is awaiting a reviewer."""
    REVISION_REQUESTED = "REVISION_REQUESTED"
    """A reviewer asked for changes; the run is queued to revise."""
    REVISING = "REVISING"
    """The agent is reworking the patch against reviewer feedback."""
    APPROVED = "APPROVED"
    """A human approved one exact patch version for publication."""
    PUBLISHING = "PUBLISHING"
    """Branch/commit/push/PR are being created from the approved patch."""
    PR_CREATED = "PR_CREATED"
    """A pull request exists. PatchPilot never goes further than this."""
    REJECTED = "REJECTED"
    """A reviewer rejected the patch. Terminal."""
    PUBLISH_FAILED = "PUBLISH_FAILED"
    """Publication stopped part-way; the recorded steps say how far it got."""

    @property
    def is_terminal(self) -> bool:
        return self in {ReviewState.PR_CREATED, ReviewState.REJECTED}

    @property
    def awaits_reviewer(self) -> bool:
        return self is ReviewState.PATCH_READY


#: Allowed transitions. Anything absent is rejected by
#: :func:`patchpilot.review.states.ensure_transition`, so an invalid move is a
#: 409 rather than a silently corrupted workflow.
REVIEW_TRANSITIONS: dict[ReviewState, frozenset[ReviewState]] = {
    ReviewState.RUNNING: frozenset({ReviewState.PATCH_READY, ReviewState.REJECTED}),
    ReviewState.PATCH_READY: frozenset(
        {
            ReviewState.APPROVED,
            ReviewState.REVISION_REQUESTED,
            ReviewState.REJECTED,
            # A re-run of the same run replaces the patch under review.
            ReviewState.RUNNING,
        }
    ),
    ReviewState.REVISION_REQUESTED: frozenset({ReviewState.REVISING, ReviewState.REJECTED}),
    ReviewState.REVISING: frozenset(
        {ReviewState.PATCH_READY, ReviewState.REJECTED, ReviewState.RUNNING}
    ),
    # Approval can be invalidated if the patch changes underneath it, which
    # sends the run back for another human decision rather than publishing
    # something nobody approved.
    ReviewState.APPROVED: frozenset(
        {ReviewState.PUBLISHING, ReviewState.PATCH_READY, ReviewState.REJECTED}
    ),
    ReviewState.PUBLISHING: frozenset({ReviewState.PR_CREATED, ReviewState.PUBLISH_FAILED}),
    ReviewState.PUBLISH_FAILED: frozenset({ReviewState.PUBLISHING, ReviewState.PATCH_READY}),
    ReviewState.PR_CREATED: frozenset(),
    ReviewState.REJECTED: frozenset(),
}


class ReviewAction(StrEnum):
    """Audit-trail verbs. One row per action, never overwritten."""

    PATCH_GENERATED = "PATCH_GENERATED"
    REVISION_REQUESTED = "REVISION_REQUESTED"
    PATCH_REVISED = "PATCH_REVISED"
    PATCH_APPROVED = "PATCH_APPROVED"
    PATCH_REJECTED = "PATCH_REJECTED"
    APPROVAL_INVALIDATED = "APPROVAL_INVALIDATED"
    PUBLISH_STARTED = "PUBLISH_STARTED"
    BRANCH_CREATED = "BRANCH_CREATED"
    COMMIT_CREATED = "COMMIT_CREATED"
    BRANCH_PUSHED = "BRANCH_PUSHED"
    PR_CREATED = "PR_CREATED"
    PUBLISH_FAILED = "PUBLISH_FAILED"


class PublicationStrategy(StrEnum):
    """How an approved patch reaches a pull request.

    Chosen by the backend from the credential's actual permissions, never by
    the agent and never by the reviewer: which repository we may write to is a
    fact about the token, not a preference.
    """

    DIRECT = "DIRECT"
    """The credential can push to upstream. Branch lives in upstream."""
    FORK = "FORK"
    """The credential cannot push to upstream. Branch lives in the
    authenticated user's fork; the pull request still targets upstream."""


class PublicationStep(StrEnum):
    """Ordered steps of a publication, recorded as each one succeeds.

    GitHub operations partially succeed all the time -- a branch lands and the
    PR call rate-limits. Recording the furthest completed step is what makes a
    retry able to skip work already done instead of duplicating it.
    """

    VERIFIED_DIGEST = "VERIFIED_DIGEST"
    RESOLVED_REPOSITORY = "RESOLVED_REPOSITORY"
    SELECTED_STRATEGY = "SELECTED_STRATEGY"
    RESOLVED_FORK = "RESOLVED_FORK"
    CHECKED_DRIFT = "CHECKED_DRIFT"
    APPLIED_PATCH = "APPLIED_PATCH"
    VERIFIED_APPLIED_DIFF = "VERIFIED_APPLIED_DIFF"
    COMMIT_CREATED = "COMMIT_CREATED"
    VERIFIED_COMMIT = "VERIFIED_COMMIT"
    BRANCH_PUSHED = "BRANCH_PUSHED"
    PR_CREATED = "PR_CREATED"


class PublishErrorCode(StrEnum):
    """Actionable failure reasons, safe to show a user.

    Never carries a token, a URL with credentials, or raw provider output.
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH_FAILED = "AUTH_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    REPOSITORY_UNKNOWN = "REPOSITORY_UNKNOWN"
    BASE_COMMIT_UNKNOWN = "BASE_COMMIT_UNKNOWN"
    BASE_BRANCH_CHANGED = "BASE_BRANCH_CHANGED"
    BRANCH_EXISTS = "BRANCH_EXISTS"
    PATCH_DIGEST_MISMATCH = "PATCH_DIGEST_MISMATCH"
    PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
    APPLIED_DIFF_MISMATCH = "APPLIED_DIFF_MISMATCH"
    PUSH_FAILED = "PUSH_FAILED"
    PR_CREATE_FAILED = "PR_CREATE_FAILED"
    PUBLICATION_COMMIT_MISMATCH = "PUBLICATION_COMMIT_MISMATCH"
    UNAPPROVED_FILE_CHANGE = "UNAPPROVED_FILE_CHANGE"
    UNAPPROVED_WORKFLOW_CHANGE = "UNAPPROVED_WORKFLOW_CHANGE"
    WORKFLOW_DRIFT = "WORKFLOW_DRIFT"
    FORK_CREATE_FAILED = "FORK_CREATE_FAILED"
    FORK_NOT_READY = "FORK_NOT_READY"
    FORK_BASE_UNAVAILABLE = "FORK_BASE_UNAVAILABLE"
    IDENTITY_UNKNOWN = "IDENTITY_UNKNOWN"
    RATE_LIMITED = "RATE_LIMITED"
    PROTECTED_BRANCH = "PROTECTED_BRANCH"
    UNKNOWN = "UNKNOWN"


class ToolOrigin(StrEnum):
    NATIVE = "native"
    MCP = "mcp"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    ERROR = "ERROR"


class BenchmarkStatus(StrEnum):
    """Verdict from the *official* SWE-bench evaluator.

    Deliberately distinct from :class:`RunState`: the agent believing its tests
    pass is not the same claim as the benchmark harness resolving the instance.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    EMPTY_PATCH = "EMPTY_PATCH"
    ERROR = "ERROR"
    NOT_EVALUATED = "NOT_EVALUATED"


class BenchmarkRunStatus(StrEnum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FailureClass(StrEnum):
    """How the agent classified the latest failing test run."""

    ORIGINAL_BUG = "ORIGINAL_BUG"
    REGRESSION = "REGRESSION"
    WRONG_ASSUMPTION = "WRONG_ASSUMPTION"
    UNRELATED = "UNRELATED"
    ENVIRONMENT = "ENVIRONMENT"
    UNKNOWN = "UNKNOWN"


# Maps the tool the agent just selected onto the state the UI should display.
TOOL_STATE_MAP: dict[str, RunState] = {
    "list_files": RunState.SEARCHING,
    "search_code": RunState.SEARCHING,
    "find_symbol": RunState.SEARCHING,
    "read_file": RunState.READING,
    "git_log": RunState.READING,
    "git_status": RunState.READING,
    "git_diff": RunState.READING,
    "record_hypothesis": RunState.HYPOTHESIZING,
    "apply_patch": RunState.PATCHING,
    "write_file": RunState.PATCHING,
    "run_tests": RunState.TESTING,
    "run_command": RunState.TESTING,
    "finish": RunState.REFLECTING,
}
