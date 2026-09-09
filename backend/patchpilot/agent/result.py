"""The value an agent run produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..domain.enums import FailureClass, RunState


@dataclass
class TestRunRecord:
    """One test execution during a run."""

    iteration: int
    command: str
    passed: int
    failed: int
    errors: int
    skipped: int
    total: int
    green: bool
    duration_ms: int
    framework: str = "unknown"
    timed_out: bool = False
    failing_tests: list[str] = field(default_factory=list)
    output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "command": self.command,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "total": self.total,
            "green": self.green,
            "duration_ms": self.duration_ms,
            "framework": self.framework,
            "timed_out": self.timed_out,
            "failing_tests": self.failing_tests,
            "output": self.output,
        }


@dataclass
class PatchAttemptRecord:
    iteration: int
    diff: str
    files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    applied: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "diff": self.diff,
            "files": self.files,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "applied": self.applied,
        }


@dataclass
class HypothesisRecord:
    iteration: int
    index: int
    suspected_cause: str
    evidence: str
    files_involved: list[str]
    proposed_fix: str
    validation_plan: str
    supersedes_previous: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "index": self.index,
            "suspected_cause": self.suspected_cause,
            "evidence": self.evidence,
            "files_involved": self.files_involved,
            "proposed_fix": self.proposed_fix,
            "validation_plan": self.validation_plan,
            "supersedes_previous": self.supersedes_previous,
        }


@dataclass
class ToolCallRecord:
    iteration: int
    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    duration_ms: int
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "summary": self.summary,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
        }


@dataclass
class RunMetrics:
    iterations: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    files_inspected: int = 0
    files_modified: int = 0
    test_runs: int = 0
    hypotheses: int = 0
    patch_attempts: int = 0
    runtime_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float | None = None
    loop_signals: int = 0

    # --- progress diagnostics -------------------------------------------------
    # Recorded so trajectories can be compared across runs. The batch that
    # motivated these had six runs reach the iteration cap without ever editing,
    # and nothing in the metrics said so: `patch_attempts` was 0, which reads
    # identically to "tried once and the patch failed to apply".
    time_to_first_edit: int | None = None
    """Iteration of the first successful edit. None means the run never edited."""
    time_to_first_file_read: int | None = None
    time_to_first_hypothesis: int | None = None
    time_to_first_test: int | None = None
    time_to_first_green_test: int | None = None
    verified_by_tests: bool = False
    """True when a `run_tests` call was observed green after the last edit.

    This is the agent's *evidence*, not its opinion, and the two disagreed on 4
    of 9 graded instances in the first batch.
    """

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "model_calls": self.model_calls,
            "files_inspected": self.files_inspected,
            "files_modified": self.files_modified,
            "test_runs": self.test_runs,
            "hypotheses": self.hypotheses,
            "patch_attempts": self.patch_attempts,
            "runtime_s": round(self.runtime_s, 2),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost_usd": self.cost_usd,
            "loop_signals": self.loop_signals,
            "time_to_first_edit": self.time_to_first_edit,
            "time_to_first_file_read": self.time_to_first_file_read,
            "time_to_first_hypothesis": self.time_to_first_hypothesis,
            "time_to_first_test": self.time_to_first_test,
            "time_to_first_green_test": self.time_to_first_green_test,
            "verified_by_tests": self.verified_by_tests,
        }


@dataclass
class AgentResult:
    """Everything one autonomous run produced."""

    run_id: str
    state: RunState
    final_patch: str = ""
    summary: str = ""
    failure_class: FailureClass | None = None
    stop_reason: str = ""
    error: str | None = None
    agent_claimed_resolved: bool = False
    """What the agent asserted via `finish`, kept apart from the evidence.

    `state` is what PatchPilot concluded and `metrics.verified_by_tests` is what
    was actually observed; the official harness remains the only authority on
    whether an instance is resolved. Keeping the three separate is what makes
    the agent's calibration measurable instead of invisible.
    """

    files_inspected: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    hypotheses: list[HypothesisRecord] = field(default_factory=list)
    patch_attempts: list[PatchAttemptRecord] = field(default_factory=list)
    test_runs: list[TestRunRecord] = field(default_factory=list)
    metrics: RunMetrics = field(default_factory=RunMetrics)

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def resolved(self) -> bool:
        """The agent's own verdict — *not* a benchmark verdict."""
        return self.state is RunState.RESOLVED

    @property
    def last_test_run(self) -> TestRunRecord | None:
        return self.test_runs[-1] if self.test_runs else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": str(self.state),
            "resolved": self.resolved,
            "final_patch": self.final_patch,
            "summary": self.summary,
            "failure_class": str(self.failure_class) if self.failure_class else None,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "files_inspected": self.files_inspected,
            "files_modified": self.files_modified,
            "metrics": self.metrics.to_dict(),
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "patch_attempts": [p.to_dict() for p in self.patch_attempts],
            "test_runs": [t.to_dict() for t in self.test_runs],
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
