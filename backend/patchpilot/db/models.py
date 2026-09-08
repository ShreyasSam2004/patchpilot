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

from ..domain.enums import BenchmarkRunStatus, BenchmarkStatus, RunState
from ..ids import new_id


def utcnow() -> datetime:
    return datetime.now(UTC)


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
    queued_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    last_seq: Mapped[int] = mapped_column(sa.Integer, default=0)

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
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

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
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)

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

    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(sa.Boolean, default=False)

    tasks: Mapped[list[BenchmarkTask]] = relationship(
        back_populates="benchmark", cascade="all, delete-orphan", order_by="BenchmarkTask.id"
    )

    @property
    def resolve_rate(self) -> float | None:
        """Resolved / attempted, as a percentage. ``None`` before evaluation.

        Deliberately returns ``None`` rather than 0.0 when nothing has been
        evaluated, so the UI can show "not evaluated" instead of "0%".
        """
        attempted = self.resolved_tasks + self.unresolved_tasks
        if not self.evaluated or attempted == 0:
            return None
        return round(self.resolved_tasks / attempted * 100, 1)


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

    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    benchmark: Mapped[BenchmarkRun] = relationship(
        back_populates="tasks", foreign_keys=[benchmark_id]
    )

    __table_args__ = (
        sa.UniqueConstraint("benchmark_id", "instance_id", name="uq_bench_instance"),
    )

    @property
    def resolved(self) -> bool:
        return self.benchmark_status == str(BenchmarkStatus.RESOLVED)
