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
