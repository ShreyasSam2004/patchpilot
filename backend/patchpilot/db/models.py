"""SQLAlchemy 2.0 ORM models.

Portable between SQLite (local development) and PostgreSQL (deployment): no
dialect-specific column types, JSON payloads via ``sa.JSON``, and timezone-aware
timestamps set in Python rather than by the database, so both backends agree.

The schema separates two verdicts that are easy to conflate and must never be:
``Run.state`` is what the *agent* concluded; ``BenchmarkTask.benchmark_status``
is what the *official SWE-bench evaluator* concluded. Only the latter feeds a
reported resolve rate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from ..domain.enums import BenchmarkRunStatus, BenchmarkStatus, ReviewState, RunState
from ..ids import new_id


def utcnow() -> datetime:
    return datetime.now(UTC)


class UtcDateTime(sa.types.TypeDecorator):
    """A datetime that always round-trips as timezone-aware UTC.

    SQLite has no native datetime type, so ``DateTime(timezone=True)`` silently
    drops the tzinfo: a value written as aware comes back naive. Mixing the two
    raises "can't subtract offset-naive and offset-aware datetimes" at the first
    piece of arithmetic — which is exactly how a benchmark batch died before it
    ran a single task. Normalising on the way in and out makes the behaviour
    identical on SQLite and PostgreSQL.
    """

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base with a JSON type map."""

    type_annotation_map = {dict[str, Any]: sa.JSON, list[str]: sa.JSON}


class Run(Base):
    """One autonomous bug-fixing run."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(sa.String(8), index=True)
    """Short human-quotable identifier shown in the UI (e.g. A82F)."""

    # -- inputs ------------------------------------------------------------
    repository: Mapped[str] = mapped_column(sa.String(500))
    repo_url: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
    local_path: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
    base_commit: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    issue: Mapped[str] = mapped_column(sa.Text)
    issue_url: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)
    issue_title: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    test_command: Mapped[str | None] = mapped_column(sa.String(1000), nullable=True)

    # -- configuration (persisted so a run is reproducible) ----------------
    provider: Mapped[str] = mapped_column(sa.String(50), default="anthropic")
    model: Mapped[str] = mapped_column(sa.String(120), default="")
    sandbox: Mapped[str] = mapped_column(sa.String(20), default="local")
    config: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)

    # -- state -------------------------------------------------------------
    state: Mapped[str] = mapped_column(sa.String(20), default=str(RunState.PENDING), index=True)
    stop_reason: Mapped[str] = mapped_column(sa.Text, default="")
    summary: Mapped[str] = mapped_column(sa.Text, default="")
    failure_class: Mapped[str | None] = mapped_column(sa.String(30), nullable=True)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # -- outputs -----------------------------------------------------------
    final_patch: Mapped[str] = mapped_column(sa.Text, default="")
    files_inspected: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    files_modified: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)

    # -- worker bookkeeping -------------------------------------------------
    queued_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True
    )
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    last_seq: Mapped[int] = mapped_column(sa.Integer, default=0)

    # -- human review ------------------------------------------------------
    review_state: Mapped[str] = mapped_column(
        sa.String(24), default=str(ReviewState.RUNNING), index=True
    )
    approved_patch_sha256: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Digest of the exact patch a human approved. Publication refuses to push
    anything whose digest differs from this."""
    approved_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)

    # -- canonical publication target, fixed when the run is created --------
    # Never re-derived later from issue text or anything the model produced:
    # a push target inferred from model-influenced text is how an agent opens a
    # pull request against a repository nobody asked about.
    repo_provider: Mapped[str | None] = mapped_column(sa.String(20), nullable=True)
    repo_owner: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    repo_name: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    base_branch: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    benchmark_task_id: Mapped[str | None] = mapped_column(
        sa.String(32), sa.ForeignKey("benchmark_tasks.id", ondelete="SET NULL"), nullable=True
    )

    events: Mapped[list[Event]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="Event.seq"
    )
    tool_calls: Mapped[list[ToolCallRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ToolCallRow.id"
    )
    hypotheses: Mapped[list[HypothesisRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="HypothesisRow.id"
    )
    patches: Mapped[list[PatchRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="PatchRow.id"
    )
    test_runs: Mapped[list[TestRunRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="TestRunRow.id"
    )
    patch_versions: Mapped[list[PatchVersionRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="PatchVersionRow.version"
    )
    review_audit: Mapped[list[ReviewAuditRow]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ReviewAuditRow.id"
    )

    __table_args__ = (
        sa.Index("ix_runs_state_queued", "state", "queued_at"),
    )

    @property
    def is_terminal(self) -> bool:
        return RunState(self.state).is_terminal


class Event(Base):
    """An append-only run event. The UI renders exclusively from these."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(sa.Integer)
    type: Mapped[str] = mapped_column(sa.String(40), index=True)
    iteration: Mapped[int] = mapped_column(sa.Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="events")

    __table_args__ = (
        sa.UniqueConstraint("run_id", "seq", name="uq_events_run_seq"),
        sa.Index("ix_events_run_seq", "run_id", "seq"),
    )


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    iteration: Mapped[int] = mapped_column(sa.Integer, default=0)
    tool: Mapped[str] = mapped_column(sa.String(100), index=True)
    origin: Mapped[str] = mapped_column(sa.String(20), default="native")
    arguments: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    ok: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    summary: Mapped[str] = mapped_column(sa.Text, default="")
    error_code: Mapped[str | None] = mapped_column(sa.String(50), nullable=True)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="tool_calls")


class HypothesisRow(Base):
    __tablename__ = "hypotheses"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    iteration: Mapped[int] = mapped_column(sa.Integer, default=0)
    index: Mapped[int] = mapped_column(sa.Integer, default=1)
    suspected_cause: Mapped[str] = mapped_column(sa.Text, default="")
    evidence: Mapped[str] = mapped_column(sa.Text, default="")
    files_involved: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    proposed_fix: Mapped[str] = mapped_column(sa.Text, default="")
    validation_plan: Mapped[str] = mapped_column(sa.Text, default="")
    supersedes_previous: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="hypotheses")


class PatchRow(Base):
    __tablename__ = "patches"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    iteration: Mapped[int] = mapped_column(sa.Integer, default=0)
    diff: Mapped[str] = mapped_column(sa.Text, default="")
    files: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    insertions: Mapped[int] = mapped_column(sa.Integer, default=0)
    deletions: Mapped[int] = mapped_column(sa.Integer, default=0)
    applied: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    is_final: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="patches")


class TestRunRow(Base):
    __tablename__ = "test_runs"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    iteration: Mapped[int] = mapped_column(sa.Integer, default=0)
    command: Mapped[str] = mapped_column(sa.Text, default="")
    framework: Mapped[str] = mapped_column(sa.String(30), default="unknown")
    passed: Mapped[int] = mapped_column(sa.Integer, default=0)
    failed: Mapped[int] = mapped_column(sa.Integer, default=0)
    errors: Mapped[int] = mapped_column(sa.Integer, default=0)
    skipped: Mapped[int] = mapped_column(sa.Integer, default=0)
    total: Mapped[int] = mapped_column(sa.Integer, default=0)
    green: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    timed_out: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(sa.Integer, default=0)
    failing_tests: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    output: Mapped[str] = mapped_column(sa.Text, default="")
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="test_runs")


class BenchmarkRun(Base):
    """One SWE-bench evaluation batch."""

    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(sa.String(200), default="")
    dataset: Mapped[str] = mapped_column(sa.String(200), default="princeton-nlp/SWE-bench_Lite")
    split: Mapped[str] = mapped_column(sa.String(40), default="test")
    run_key: Mapped[str] = mapped_column(sa.String(120), index=True)
    """The ``--run_id`` handed to the official harness; ties our rows to its logs."""

    provider: Mapped[str] = mapped_column(sa.String(50), default="anthropic")
    model: Mapped[str] = mapped_column(sa.String(120), default="")
    config: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    instance_ids: Mapped[list[str]] = mapped_column(sa.JSON, default=list)

    status: Mapped[str] = mapped_column(
        sa.String(20), default=str(BenchmarkRunStatus.PENDING), index=True
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    total_tasks: Mapped[int] = mapped_column(sa.Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(sa.Integer, default=0)
    resolved_tasks: Mapped[int] = mapped_column(sa.Integer, default=0)
    unresolved_tasks: Mapped[int] = mapped_column(sa.Integer, default=0)
    error_tasks: Mapped[int] = mapped_column(sa.Integer, default=0)
    empty_patch_tasks: Mapped[int] = mapped_column(sa.Integer, default=0)

    evaluated: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    """True only when the official harness produced a report for this batch."""
    evaluator_detail: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)

    aggregate: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    tasks: Mapped[list[BenchmarkTask]] = relationship(
        back_populates="benchmark", cascade="all, delete-orphan", order_by="BenchmarkTask.id"
    )

    @property
    def resolve_rate(self) -> float | None:
        """Resolved / attempted, as a percentage. ``None`` before evaluation.

        The denominator is every task the batch took on, which is the SWE-bench
        convention: an instance the agent produced no patch for, or that errored
        in the harness, is a task it failed, not a task that does not count.
        Dividing by graded patches alone turns "gave up on 6 of 15" into a
        higher score than "attempted all 15", which is backwards.

        Deliberately returns ``None`` rather than 0.0 when nothing has been
        evaluated, so the UI can show "not evaluated" instead of "0%".
        """
        if not self.evaluated or self.attempted_tasks == 0:
            return None
        return round(self.resolved_tasks / self.attempted_tasks * 100, 1)

    @property
    def attempted_tasks(self) -> int:
        """Denominator for the resolve rate: every task the batch took on.

        One source of truth, so a caption can never quote a count that does not
        match the percentage printed beside it.
        """
        return (
            self.resolved_tasks
            + self.unresolved_tasks
            + self.empty_patch_tasks
            + self.error_tasks
        )


class BenchmarkTask(Base):
    """One SWE-bench instance inside a batch."""

    __tablename__ = "benchmark_tasks"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=new_id)
    benchmark_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("benchmark_runs.id", ondelete="CASCADE"), index=True
    )
    instance_id: Mapped[str] = mapped_column(sa.String(200), index=True)
    repo: Mapped[str] = mapped_column(sa.String(200), default="")
    base_commit: Mapped[str] = mapped_column(sa.String(64), default="")
    problem_statement: Mapped[str] = mapped_column(sa.Text, default="")
    environment_setup_commit: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    run_id: Mapped[str | None] = mapped_column(sa.String(32), nullable=True, index=True)
    """The agent Run that produced this task's patch."""

    agent_status: Mapped[str] = mapped_column(sa.String(20), default=str(RunState.PENDING))
    """What the agent itself concluded. NOT a benchmark verdict."""

    benchmark_status: Mapped[str] = mapped_column(
        sa.String(20), default=str(BenchmarkStatus.PENDING), index=True
    )
    """The official SWE-bench evaluator's verdict. Authoritative for reporting."""

    model_patch: Mapped[str] = mapped_column(sa.Text, default="")
    evaluator_report: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    benchmark: Mapped[BenchmarkRun] = relationship(
        back_populates="tasks", foreign_keys=[benchmark_id]
    )

    __table_args__ = (
        sa.UniqueConstraint("benchmark_id", "instance_id", name="uq_bench_instance"),
    )

    @property
    def resolved(self) -> bool:
        return self.benchmark_status == str(BenchmarkStatus.RESOLVED)


class PatchVersionRow(Base):
    """One patch offered for human review.

    Distinct from :class:`PatchRow`, which records what the agent applied on a
    given iteration. This is the reviewer-facing artefact: the complete diff as
    proposed, its digest, the verification behind it, and what the reviewer
    decided. Versions are never overwritten -- a revision adds V2 beside V1 --
    because the audit question "what exactly was approved, and what had been
    rejected before it" cannot be answered from a mutated row.
    """

    __tablename__ = "patch_versions"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(sa.Integer)
    diff: Mapped[str] = mapped_column(sa.Text, default="")
    digest: Mapped[str] = mapped_column(sa.String(64), index=True)
    """SHA-256 of the canonical patch. What approval binds to."""

    files: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    insertions: Mapped[int] = mapped_column(sa.Integer, default=0)
    deletions: Mapped[int] = mapped_column(sa.Integer, default=0)
    patch_quality: Mapped[str] = mapped_column(sa.String(20), default="no_patch")
    verification: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    """Test evidence as it stood for this version: counts, framework, whether
    the runner's summary was actually recognised."""

    model: Mapped[str] = mapped_column(sa.String(120), default="")
    iterations: Mapped[int] = mapped_column(sa.Integer, default=0)
    root_cause: Mapped[str] = mapped_column(sa.Text, default="")
    repair_summary: Mapped[str] = mapped_column(sa.Text, default="")

    decision: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    reviewer_feedback: Mapped[str] = mapped_column(sa.Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="patch_versions")

    __table_args__ = (
        sa.UniqueConstraint("run_id", "version", name="uq_patch_versions_run_version"),
        sa.Index("ix_patch_versions_run_version", "run_id", "version"),
    )


class PublicationRow(Base):
    """One attempt to publish one approved patch.

    Unique on ``(run_id, patch_digest)``: that constraint, not a disabled
    button, is what stops a double-clicked Approve from opening two pull
    requests. A retry finds the existing row and resumes from
    :attr:`completed_steps` rather than repeating work that already landed.
    """

    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    patch_version_id: Mapped[int | None] = mapped_column(
        sa.Integer, sa.ForeignKey("patch_versions.id", ondelete="SET NULL"), nullable=True
    )
    patch_digest: Mapped[str] = mapped_column(sa.String(64), index=True)
    state: Mapped[str] = mapped_column(sa.String(24), default=str(ReviewState.PUBLISHING))

    # -- target, captured at publication time -------------------------------
    provider: Mapped[str] = mapped_column(sa.String(20), default="github")
    owner: Mapped[str] = mapped_column(sa.String(120), default="")
    repo: Mapped[str] = mapped_column(sa.String(120), default="")
    base_branch: Mapped[str] = mapped_column(sa.String(255), default="")
    base_commit_sha: Mapped[str] = mapped_column(sa.String(64), default="")
    observed_base_head: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Where the base branch actually pointed when we published. Differs from
    :attr:`base_commit_sha` when the branch moved while the agent worked."""
    base_drifted: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    # -- results ------------------------------------------------------------
    branch: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    pr_url: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)
    pr_state: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    checks: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)

    completed_steps: Mapped[list[str]] = mapped_column(sa.JSON, default=list)
    error_code: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("run_id", "patch_digest", name="uq_publications_run_digest"),
    )

    def has_completed(self, step: str) -> bool:
        return step in (self.completed_steps or [])


class ReviewAuditRow(Base):
    """Append-only record of every review and publication action.

    Separate from the run event stream because it answers a different question
    and must survive independently: who approved which digest, against which
    base, and what was created as a result.
    """

    __tablename__ = "review_audit"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        sa.String(32), sa.ForeignKey("runs.id", ondelete="CASCADE"), index=True
    )
    patch_version: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    action: Mapped[str] = mapped_column(sa.String(32), index=True)
    actor: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    feedback: Mapped[str] = mapped_column(sa.Text, default="")
    patch_digest: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="review_audit")
