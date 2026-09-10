"""API request/response models.

DTOs are separate from ORM models on purpose: the wire format is a contract the
frontend depends on, and it should not change just because a column is renamed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..db.models import (
    BenchmarkRun,
    BenchmarkTask,
    PatchVersionRow,
    PublicationRow,
    Run,
)


# --------------------------------------------------------------------- runs
class CreateRunRequest(BaseModel):
    issue: str = Field(min_length=10, description="The bug report or issue body.")
    repo_url: str | None = Field(default=None, description="HTTPS URL of a git repository.")
    local_path: str | None = Field(default=None, description="Path to a local checkout.")
    fixture: str | None = Field(default=None, description="Built-in fixture id to run against.")
    repository: str | None = None
    base_commit: str | None = None
    issue_url: str | None = None
    issue_title: str | None = None
    test_command: str | None = None

    provider: str | None = None
    model: str | None = None
    sandbox: Literal["local", "docker"] | None = None
    docker_image: str | None = Field(
        default=None,
        description=(
            "Sandbox image for this run. The docker sandbox has no network, so the "
            "image must already contain git, a test runner, and the repository's "
            "dependencies. Defaults to the configured image."
        ),
    )
    max_iterations: int | None = Field(default=None, ge=1, le=200)
    max_tool_calls: int | None = Field(default=None, ge=1, le=2000)
    max_runtime_s: int | None = Field(default=None, ge=30, le=21_600)

    @field_validator("repo_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value and not value.startswith(("https://", "http://", "git@")):
            raise ValueError("repo_url must be an http(s) or ssh git URL")
        return value

    @model_validator(mode="after")
    def _needs_a_source(self) -> CreateRunRequest:
        if not any((self.repo_url, self.local_path, self.fixture)):
            raise ValueError("provide one of: repo_url, local_path, fixture")
        return self


class MetricsOut(BaseModel):
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


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    repository: str
    issue_title: str | None
    issue_url: str | None
    state: str
    summary: str
    provider: str
    model: str
    sandbox: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    metrics: dict[str, Any] = {}
    files_modified: list[str] = []
    has_patch: bool = False
    is_benchmark_task: bool = False
    review_state: str = "RUNNING"

    @classmethod
    def of(cls, run: Run) -> RunSummaryOut:
        return cls(
            id=run.id,
            code=run.code,
            repository=run.repository,
            issue_title=run.issue_title or _first_line(run.issue),
            issue_url=run.issue_url,
            state=run.state,
            review_state=run.review_state,
            summary=run.summary,
            provider=run.provider,
            model=run.model,
            sandbox=run.sandbox,
            queued_at=run.queued_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            metrics=run.metrics or {},
            files_modified=run.files_modified or [],
            has_patch=bool((run.final_patch or "").strip()),
            is_benchmark_task=run.benchmark_task_id is not None,
        )


class RunDetailOut(RunSummaryOut):
    issue: str
    repo_url: str | None
    base_commit: str | None
    test_command: str | None
    stop_reason: str
    failure_class: str | None
    error: str | None
    config: dict[str, Any] = {}
    files_inspected: list[str] = []
    final_patch: str = ""
    last_seq: int = 0
    cancel_requested: bool = False

    @classmethod
    def of(cls, run: Run) -> RunDetailOut:
        base = RunSummaryOut.of(run).model_dump()
        return cls(
            **base,
            issue=run.issue,
            repo_url=run.repo_url,
            base_commit=run.base_commit,
            test_command=run.test_command,
            stop_reason=run.stop_reason,
            failure_class=run.failure_class,
            error=run.error,
            config=run.config or {},
            files_inspected=run.files_inspected or [],
            final_patch=run.final_patch or "",
            last_seq=run.last_seq,
            cancel_requested=run.cancel_requested,
        )


class RunListOut(BaseModel):
    items: list[RunSummaryOut]
    total: int
    limit: int
    offset: int


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    seq: int
    type: str
    iteration: int
    payload: dict[str, Any]
    created_at: datetime


class ToolCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    iteration: int
    tool: str
    origin: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    error_code: str | None
    duration_ms: int
    created_at: datetime


class TestRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    iteration: int
    command: str
    framework: str
    passed: int
    failed: int
    errors: int
    skipped: int
    total: int
    green: bool
    timed_out: bool
    duration_ms: int
    failing_tests: list[str]
    output: str
    created_at: datetime


class PatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    iteration: int
    diff: str
    files: list[str]
    insertions: int
    deletions: int
    applied: bool
    is_final: bool
    created_at: datetime


class HypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    iteration: int
    index: int
    suspected_cause: str
    evidence: str
    files_involved: list[str]
    proposed_fix: str
    validation_plan: str
    supersedes_previous: bool
    created_at: datetime


class DiffFileOut(BaseModel):
    """One file in a parsed unified diff, ready to render."""

    path: str
    old_path: str
    additions: int
    deletions: int
    is_new: bool
    is_deleted: bool
    hunks: list[dict[str, Any]]


class DiffOut(BaseModel):
    raw: str
    files: list[DiffFileOut]
    additions: int
    deletions: int


# ---------------------------------------------------------------- benchmark
class CreateBenchmarkRequest(BaseModel):
    name: str | None = None
    dataset: str | None = None
    split: str | None = None
    limit: int | None = Field(default=None, ge=1, le=300, description="How many instances to run.")
    instance_ids: list[str] | None = Field(default=None, description="Explicit instance ids.")
    offset: int = Field(default=0, ge=0)
    provider: str | None = None
    model: str | None = None
    sandbox: Literal["local", "docker"] | None = None
    max_iterations: int | None = Field(default=None, ge=1, le=200)
    evaluate: bool = Field(
        default=True,
        description="Run the official SWE-bench evaluator after generating patches.",
    )

    @model_validator(mode="after")
    def _needs_a_selection(self) -> CreateBenchmarkRequest:
        if not self.limit and not self.instance_ids:
            raise ValueError("provide either limit or instance_ids")
        return self


class BenchmarkTaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    instance_id: str
    repo: str
    base_commit: str
    run_id: str | None
    agent_status: str
    benchmark_status: str
    has_patch: bool = False
    metrics: dict[str, Any] = {}
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def of(cls, task: BenchmarkTask) -> BenchmarkTaskOut:
        return cls(
            id=task.id,
            instance_id=task.instance_id,
            repo=task.repo,
            base_commit=task.base_commit,
            run_id=task.run_id,
            agent_status=task.agent_status,
            benchmark_status=task.benchmark_status,
            has_patch=bool((task.model_patch or "").strip()),
            metrics=task.metrics or {},
            error=task.error,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )


class BenchmarkTaskDetailOut(BenchmarkTaskOut):
    problem_statement: str = ""
    model_patch: str = ""
    evaluator_report: dict[str, Any] = {}

    @classmethod
    def of(cls, task: BenchmarkTask) -> BenchmarkTaskDetailOut:
        base = BenchmarkTaskOut.of(task).model_dump()
        return cls(
            **base,
            problem_statement=task.problem_statement,
            model_patch=task.model_patch or "",
            evaluator_report=task.evaluator_report or {},
        )


class BenchmarkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    dataset: str
    split: str
    run_key: str
    provider: str
    model: str
    config: dict[str, Any]
    status: str
    error: str | None
    total_tasks: int
    completed_tasks: int
    resolved_tasks: int
    unresolved_tasks: int
    error_tasks: int
    empty_patch_tasks: int
    evaluated: bool
    attempted_tasks: int
    """Denominator of ``resolve_rate``: resolved + unresolved + empty + error.

    Exposed so a caption cannot derive its own total and print a count that
    disagrees with the percentage next to it."""
    resolve_rate: float | None
    aggregate: dict[str, Any]
    evaluator_detail: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, benchmark: BenchmarkRun) -> BenchmarkOut:
        return cls(
            id=benchmark.id,
            name=benchmark.name,
            dataset=benchmark.dataset,
            split=benchmark.split,
            run_key=benchmark.run_key,
            provider=benchmark.provider,
            model=benchmark.model,
            config=benchmark.config or {},
            status=benchmark.status,
            error=benchmark.error,
            total_tasks=benchmark.total_tasks,
            completed_tasks=benchmark.completed_tasks,
            resolved_tasks=benchmark.resolved_tasks,
            unresolved_tasks=benchmark.unresolved_tasks,
            error_tasks=benchmark.error_tasks,
            empty_patch_tasks=benchmark.empty_patch_tasks,
            evaluated=benchmark.evaluated,
            attempted_tasks=benchmark.attempted_tasks,
            resolve_rate=benchmark.resolve_rate,
            aggregate=benchmark.aggregate or {},
            evaluator_detail=benchmark.evaluator_detail or {},
            created_at=benchmark.created_at,
            started_at=benchmark.started_at,
            finished_at=benchmark.finished_at,
        )


class BenchmarkListOut(BaseModel):
    items: list[BenchmarkOut]
    total: int


# ----------------------------------------------------------------- settings
class SystemStatusOut(BaseModel):
    version: str
    environment: str
    provider: dict[str, Any]
    sandbox: dict[str, Any]
    swebench: dict[str, Any]
    tools: list[dict[str, Any]]
    policy: dict[str, Any]
    mcp: dict[str, Any]
    budgets: dict[str, Any]
    database: str
    queue_depth: int
    warnings: list[str]


class FixtureOut(BaseModel):
    id: str
    title: str
    issue: str
    repository: str
    test_command: str
    expected_failing_tests: list[str]
    expected_files: list[str]
    difficulty: str
    notes: str


def _first_line(text: str, limit: int = 140) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


# ------------------------------------------------------------------- review


class PatchVersionOut(BaseModel):
    """One patch offered for review. Versions are never overwritten."""

    model_config = ConfigDict(from_attributes=True)

    version: int
    digest: str
    files: list[str] = []
    insertions: int = 0
    deletions: int = 0
    patch_quality: str = "no_patch"
    verification: dict[str, Any] = {}
    model: str = ""
    iterations: int = 0
    root_cause: str = ""
    repair_summary: str = ""
    decision: str | None = None
    reviewer_feedback: str = ""
    decided_at: datetime | None = None
    decided_by: str | None = None
    created_at: datetime
    diff: str = ""

    @classmethod
    def of(cls, row: PatchVersionRow, *, include_diff: bool = False) -> PatchVersionOut:
        return cls(
            version=row.version,
            digest=row.digest,
            files=row.files or [],
            insertions=row.insertions,
            deletions=row.deletions,
            patch_quality=row.patch_quality,
            verification=row.verification or {},
            model=row.model,
            iterations=row.iterations,
            root_cause=row.root_cause,
            repair_summary=row.repair_summary,
            decision=row.decision,
            reviewer_feedback=row.reviewer_feedback,
            decided_at=row.decided_at,
            decided_by=row.decided_by,
            created_at=row.created_at,
            diff=row.diff if include_diff else "",
        )


class PublicationOut(BaseModel):
    """What happened, or is happening, on GitHub."""

    model_config = ConfigDict(from_attributes=True)

    state: str
    provider: str = "github"
    strategy: str = "DIRECT"
    """DIRECT or FORK. Informational: the backend decides it, never the user."""
    owner: str = ""
    repo: str = ""
    head_owner: str | None = None
    head_repo: str | None = None
    """Where the branch lives. Differs from owner/repo only for a fork."""
    base_branch: str = ""
    base_commit_sha: str = ""
    observed_base_head: str | None = None
    base_drifted: bool = False
    branch: str | None = None
    commit_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    checks: dict[str, Any] = {}
    completed_steps: list[str] = []
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: PublicationRow) -> PublicationOut:
        return cls.model_validate(row)


class ReviewAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: str
    patch_version: int | None = None
    actor: str | None = None
    feedback: str = ""
    patch_digest: str | None = None
    detail: dict[str, Any] = {}
    created_at: datetime


class ReviewOut(BaseModel):
    """Everything the review screen needs in one request."""

    run_id: str
    code: str
    repository: str
    repo_owner: str | None = None
    repo_name: str | None = None
    base_branch: str | None = None
    base_commit: str | None = None
    issue_title: str | None = None
    issue_url: str | None = None
    model: str = ""
    run_state: str
    review_state: str
    approved_patch_sha256: str | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    can_approve: bool = False
    can_publish: bool = False
    publishing_available: bool = False
    publishing_unavailable_reason: str | None = None
    current: PatchVersionOut | None = None
    versions: list[PatchVersionOut] = []
    publication: PublicationOut | None = None
    audit: list[ReviewAuditOut] = []


class ReviewDecisionIn(BaseModel):
    """Body for approve / request-revision / reject."""

    feedback: str = Field(default="", max_length=20_000)
    reviewer: str | None = Field(default=None, max_length=200)
    expected_digest: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Digest of the patch the reviewer was looking at. When supplied and "
            "stale, the decision is refused rather than applied to a patch the "
            "reviewer never saw."
        ),
    )
