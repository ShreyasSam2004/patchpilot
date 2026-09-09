"""Process tools: record_hypothesis and finish.

These make the agent's engineering discipline explicit and inspectable. The
hypothesis tool is how a *structured* claim ("suspected cause / evidence /
files / proposed fix / validation plan") enters the record — as opposed to
hidden reasoning, which is never stored.

``finish`` is the only way to end a run successfully, and it is guarded: the
orchestrator refuses it when there is no patch, or when the last test run was
not green.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...domain.enums import FailureClass
from ...repair.patchquality import PatchAssessment, assess_patch
from ..base import Tool, ToolContext, ToolResult


class RecordHypothesisArgs(BaseModel):
    suspected_cause: str = Field(
        min_length=3, description="One or two sentences naming the root cause."
    )
    evidence: str = Field(
        min_length=3,
        description="What you observed that supports this — file paths, line numbers, "
        "test output, stack frames.",
    )
    files_involved: list[str] = Field(
        default_factory=list, description="Repository-relative paths you expect to change."
    )
    proposed_fix: str = Field(min_length=3, description="The minimal change you intend to make.")
    validation_plan: str = Field(
        min_length=3, description="The exact test command that will prove the fix."
    )
    supersedes_previous: bool = Field(
        default=False,
        description="Set when this replaces an earlier hypothesis that the evidence disproved.",
    )


class RecordHypothesisTool(Tool[RecordHypothesisArgs]):
    name = "record_hypothesis"
    description = (
        "Record your structured hypothesis about the root cause BEFORE editing code. "
        "State the suspected cause, the evidence for it, the files involved, the minimal fix, "
        "and how you will validate it. Record a new one whenever the evidence contradicts "
        "the current hypothesis."
    )
    args_model = RecordHypothesisArgs
    timeout_s = 10.0

    async def execute(self, ctx: ToolContext, args: RecordHypothesisArgs) -> ToolResult:
        for path in args.files_involved:
            try:
                ctx.workspace.resolve(path)
            except Exception:
                return ToolResult.failure(
                    f"files_involved contains a path outside the repository: {path}",
                    code="path_escape",
                )

        index = int(ctx.state.get("hypothesis_count", 0)) + 1
        ctx.state["hypothesis_count"] = index
        ctx.state["current_hypothesis"] = args.model_dump()

        if ctx.emit is not None:
            await ctx.emit.hypothesis(
                index=index,
                cause=args.suspected_cause,
                evidence=args.evidence,
                files=args.files_involved,
                fix=args.proposed_fix,
                validation=args.validation_plan,
                supersedes=args.supersedes_previous,
            )

        return ToolResult.success(
            f"Hypothesis #{index} recorded. Now make the minimal change in "
            f"{', '.join(args.files_involved) or 'the relevant file'} and validate with: "
            f"{args.validation_plan}",
            summary=f"hypothesis #{index}: {args.suspected_cause[:120]}",
            index=index,
        )


class FinishArgs(BaseModel):
    resolved: bool = Field(
        description="True if you believe the issue is fixed and validated by passing tests."
    )
    summary: str = Field(
        min_length=3, description="What was wrong and what you changed, in two or three sentences."
    )
    failure_class: FailureClass | None = Field(
        default=None,
        description="When resolved=false, how you would classify the remaining failure.",
    )
    remaining_concerns: str | None = Field(
        default=None, description="Anything a reviewer should check."
    )


def _should_push_for_a_repair(ctx: ToolContext, assessment: PatchAssessment) -> bool:
    """Whether to refuse one give-up and ask for the repair the diagnosis implies.

    Deliberately at most once per run, and only while there is budget to act on
    it. An agent that has genuinely exhausted its options must be able to say so:
    trapping it in a loop would be worse than the empty patch, and it can finish
    immediately by calling again.
    """
    if ctx.state.get("repair_push_used"):
        return False
    if assessment.is_repair:
        return False
    if not ctx.state.get("repair_has_diagnosis"):
        return False
    remaining = ctx.state.get("budget_fraction_remaining")
    return isinstance(remaining, (int, float)) and remaining >= 0.15


class FinishTool(Tool[FinishArgs]):
    name = "finish"
    description = (
        "End the run. Call with resolved=true only after a test run has actually passed and "
        "the diff contains your fix. Call with resolved=false if you are confident you cannot "
        "resolve it — explain why rather than looping."
    )
    args_model = FinishArgs
    timeout_s = 10.0

    async def execute(self, ctx: ToolContext, args: FinishArgs) -> ToolResult:
        diff = await ctx.workspace.diff()
        outcome = ctx.state.get("last_test_outcome")
        assessment = assess_patch(diff)

        if args.resolved:
            if not diff.strip():
                return ToolResult.failure(
                    "Cannot finish as resolved: the working tree has no changes. "
                    "Apply your fix first.",
                    code="no_patch",
                )
            if not assessment.is_repair:
                # One run submitted a debug script as its entire patch and was
                # recorded as having produced one. Investigation debris is not a
                # repair, whatever the agent believes about it.
                return ToolResult.failure(
                    f"Cannot finish as resolved: {assessment.describe()}. "
                    "A run only counts if the diff changes implementation code. "
                    "Convert your diagnosis into a source edit.",
                    code="no_meaningful_patch",
                )
            if outcome is None:
                return ToolResult.failure(
                    "Cannot finish as resolved: you have not run the tests yet. "
                    "Call run_tests to validate the fix.",
                    code="untested",
                )
            if not outcome.green:
                return ToolResult.failure(
                    f"Cannot finish as resolved: the last test run was not green "
                    f"({outcome.describe()}). Fix the failures or finish with resolved=false.",
                    code="tests_failing",
                )
            if ctx.state.get("tests_stale"):
                # The green run above happened before the most recent edit, so
                # it says nothing about the tree being submitted. Without this
                # the guard could be satisfied by a pass that predates the code
                # it is supposed to be validating.
                return ToolResult.failure(
                    "Cannot finish as resolved: you edited the code after your last test "
                    "run, so that green result no longer covers the current diff. "
                    "Run the tests again.",
                    code="tests_stale",
                )

        if not args.resolved and _should_push_for_a_repair(ctx, assessment):
            ctx.state["repair_push_used"] = True
            candidate = ctx.state.get("repair_candidate_file")
            where = f" Start with `{candidate}`." if candidate else ""
            return ToolResult.failure(
                "Not so fast: you have a working diagnosis but "
                f"{assessment.describe()}, and you still have budget left. "
                "Every empty-patch run in the last benchmark named the right file "
                "in its summary and never edited it -- a diagnosis that stays in "
                f"prose scores nothing.{where} Make the smallest source change "
                "your diagnosis implies, then re-run the check. If you truly "
                "cannot, call finish again and say what specific unknown blocked "
                "you.",
                code="diagnosis_without_repair",
            )

        ctx.state["finish_requested"] = True
        ctx.state["finish_resolved"] = bool(args.resolved)
        ctx.state["finish_summary"] = args.summary
        ctx.state["finish_failure_class"] = (
            str(args.failure_class) if args.failure_class else None
        )
        ctx.state["finish_concerns"] = args.remaining_concerns

        verdict = "RESOLVED" if args.resolved else "NOT RESOLVED"
        return ToolResult.success(
            f"Run finished: {verdict}. {args.summary}",
            summary=f"finish — {verdict}",
            resolved=bool(args.resolved),
        )
