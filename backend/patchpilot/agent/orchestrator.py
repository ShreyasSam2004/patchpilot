"""The autonomous loop.

One iteration is: ask the model what to do next, execute the tools it asked
for, feed the observations back, and decide whether to continue. The loop ends
when the agent calls ``finish``, when a budget is exhausted, or when it is
cancelled.

Three properties matter more than the code volume here:

* **Test feedback is mandatory.** ``finish(resolved=True)`` is refused unless a
  test run actually went green, so the agent cannot declare victory it did not
  observe.
* **Repetition is interrupted, not tolerated.** The loop detector injects a
  re-plan directive rather than waiting for the iteration budget to run out.
* **State is always recoverable.** Every decision emits an event before the
  next one starts, so a crashed or cancelled run still has a complete history.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..domain.enums import TOOL_STATE_MAP, FailureClass, RunState
from ..errors import ProviderError
from ..events.bus import EventBus, RunEmitter
from ..logging_setup import get_logger
from ..providers.base import ModelProvider, ToolCallResult
from ..sandbox.base import Sandbox
from ..tools.base import ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from ..workspace.workspace import Workspace
from .antiloop import LoopDetector
from .budget import Budget, BudgetTracker
from .prompts import (
    budget_warning_message,
    build_system_prompt,
    continuation_message,
    failure_classification_hint,
    initial_user_message,
    recovery_message,
)
from .result import (
    AgentResult,
    HypothesisRecord,
    PatchAttemptRecord,
    RunMetrics,
    TestRunRecord,
    ToolCallRecord,
)
from .transcript import Transcript

log = get_logger(__name__)


@dataclass
class AgentTask:
    """The unit of work handed to the orchestrator."""

    run_id: str
    issue: str
    repository: str = "local"
    repo_url: str | None = None
    local_path: Path | None = None
    base_commit: str | None = None
    test_command: str | None = None
    hints: str | None = None
    extra_guidance: str | None = None
    budget_overrides: dict[str, Any] = field(default_factory=dict)


class AgentOrchestrator:
    """Runs one autonomous bug-fixing task."""

    def __init__(
        self,
        *,
        task: AgentTask,
        workspace: Workspace,
        sandbox: Sandbox,
        registry: ToolRegistry,
        provider: ModelProvider,
        settings: Settings,
        bus: EventBus,
    ) -> None:
        self.task = task
        self.workspace = workspace
        self.sandbox = sandbox
        self.registry = registry
        self.provider = provider
        self.settings = settings
        self.emitter = RunEmitter(bus)

        self.budget = BudgetTracker(Budget.from_settings(settings).merged(task.budget_overrides))
        self.detector = LoopDetector()
        self.transcript = Transcript(max_result_chars=settings.max_tool_output_bytes)

        self.state: RunState = RunState.PENDING
        self.tool_state: dict[str, Any] = {"test_command": task.test_command}
        self.result = AgentResult(run_id=task.run_id, state=RunState.PENDING)
        self._files_modified: set[str] = set()
        self._cancelled = False
        self._pending_directives: list[str] = []

    # ------------------------------------------------------------------ state
    async def _set_state(self, state: RunState) -> None:
        if state is self.state:
            return
        previous, self.state = self.state, state
        await self.emitter.state_changed(state, previous)

    def cancel(self) -> None:
        self._cancelled = True

    # ------------------------------------------------------------------- run
    async def run(self) -> AgentResult:
        started_monotonic = time.monotonic()
        self.result.started_at = datetime.now(UTC)
        try:
            await self._prepare()
            await self._loop()
        except asyncio.CancelledError:
            await self._finalize(RunState.CANCELLED, "run was cancelled")
            raise
        except ProviderError as exc:
            log.error("agent.provider_error", run_id=self.task.run_id, error=str(exc))
            self.result.error = str(exc)
            await self._finalize(RunState.FAILED, f"model provider error: {exc}")
        except Exception as exc:
            log.exception("agent.crashed", run_id=self.task.run_id)
            self.result.error = f"{type(exc).__name__}: {exc}"
            await self._finalize(RunState.FAILED, f"unexpected error: {exc}")
        finally:
            self.result.metrics.runtime_s = time.monotonic() - started_monotonic
            self.result.finished_at = datetime.now(UTC)
        return self.result

    async def _prepare(self) -> None:
        await self._set_state(RunState.PREPARING)
        info = await self.workspace.info()
        await self.emitter.repository_ready(
            repository=self.task.repository,
            head=info.head,
            branch=info.branch,
            base_commit=self.task.base_commit,
            sandbox=self.sandbox.info().kind,
            isolation=self.sandbox.info().isolation,
        )

        if self.task.test_command is None:
            discovered = await self._discover_test_command()
            self.tool_state["test_command"] = discovered
            await self.emitter.warning(
                "no test command supplied; using a discovered default",
                test_command=discovered,
            )

        await self._set_state(RunState.ANALYZING)
        await self.emitter.issue_analyzed(
            issue_length=len(self.task.issue),
            tools_available=self.registry.allowed_names,
            tools_blocked=self.registry.blocked_names,
            test_command=self.tool_state.get("test_command"),
        )

        self.transcript.add_user(initial_user_message(self.budget.describe_remaining()))

    async def _discover_test_command(self) -> str:
        """Pick a plausible default test command from the repository layout."""
        root = self.workspace.root
        if (root / "package.json").exists() and not any(root.glob("**/test_*.py")):
            return "npm test"
        for marker in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py"):
            if (root / marker).exists():
                return "python -m pytest -q"
        if (root / "tests").exists() or (root / "test").exists():
            return "python -m pytest -q"
        return "python -m pytest -q"

    # ------------------------------------------------------------- main loop
    async def _loop(self) -> None:
        system_prompt = build_system_prompt(
            repository=self.task.repository,
            issue=self.task.issue,
            test_command=self.tool_state.get("test_command"),
            base_commit=self.task.base_commit,
            hints=self.task.hints,
            extra_guidance=self.task.extra_guidance,
        )
        tool_specs = [spec.to_dict() for spec in self.registry.specs()]

        while True:
            if self._cancelled:
                await self._finalize(RunState.CANCELLED, "cancelled by operator")
                return

            limit = self.budget.exceeded()
            if limit:
                await self.emitter.budget_exceeded(limit=limit, snapshot=self.budget.snapshot())
                await self._finalize(
                    RunState.TIMEOUT, f"stopped after reaching the {limit} limit"
                )
                return

            self.budget.start_iteration()
            iteration = self.budget.iterations
            self.emitter.set_iteration(iteration)
            await self.emitter.iteration_started(iteration, self.budget.snapshot())

            self._inject_pending_directives()
            self._maybe_warn_about_budget()

            response = await self._call_model(system_prompt, tool_specs)
            if response is None:
                return

            await self.emitter.agent_message(response.text)
            self.transcript.add_assistant(response.text, response.tool_calls)

            if not response.tool_calls:
                # A turn with no tool call wastes budget; ask for an action.
                self.transcript.add_user(
                    "You did not call a tool. Every turn must take a concrete action. "
                    "Call the next tool now, or call finish if you are done."
                )
                await self.emitter.iteration_completed(iteration, tool_calls=0, mutated=False)
                continue

            mutated = await self._execute_tool_calls(response.tool_calls, iteration)

            if self.tool_state.get("finish_requested"):
                await self._handle_finish()
                return

            self.detector.end_iteration(mutated=mutated)
            await self._check_for_loops()

            self.transcript.add_user(
                continuation_message(
                    self.budget.describe_remaining(),
                    has_patch=bool(self._files_modified),
                    test_summary=self._last_test_summary(),
                )
            )
            await self.emitter.iteration_completed(
                iteration,
                tool_calls=len(response.tool_calls),
                mutated=mutated,
                files_modified=sorted(self._files_modified),
            )

    async def _call_model(self, system_prompt: str, tool_specs: list[dict[str, Any]]):
        await self.emitter.model_call_started(
            self.provider.model, self.transcript.approx_tokens()
        )
        try:
            response = await self.provider.complete(
                system=system_prompt,
                messages=self.transcript.render(),
                tools=tool_specs,
                max_tokens=self.settings.max_output_tokens,
                temperature=self.settings.temperature,
            )
        except ProviderError as exc:
            self.result.error = str(exc)
            await self._finalize(RunState.FAILED, f"model provider error: {exc}")
            return None

        self.result.metrics.model_calls += 1
        self.result.metrics.input_tokens += response.usage.input_tokens
        self.result.metrics.output_tokens += response.usage.output_tokens
        self.result.metrics.cached_tokens += response.usage.cache_read_tokens
        self.budget.record_cost(response.usage.cost_usd)

        await self.emitter.model_call_finished(
            model=response.model or self.provider.model,
            latency_ms=response.latency_ms,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_tokens": response.usage.cache_read_tokens,
                "cost_usd": response.usage.cost_usd,
            },
            stop_reason=response.stop_reason,
        )
        return response

    async def _execute_tool_calls(self, calls: list[Any], iteration: int) -> bool:
        """Run every requested tool, emit events, and return whether code changed."""
        results: list[ToolCallResult] = []
        mutated = False

        for call in calls:
            self.budget.record_tool_call()
            await self._set_state(TOOL_STATE_MAP.get(call.name, RunState.ANALYZING))
            await self.emitter.tool_called(
                tool=call.name, arguments=call.arguments, call_id=call.id
            )

            ctx = ToolContext(
                workspace=self.workspace,
                sandbox=self.sandbox,
                settings=self.settings,
                emit=self.emitter,
                state=self.tool_state,
            )
            ctx.iteration = iteration

            tool_result = await self.registry.call(call.name, ctx, call.arguments)
            is_mutating = self._is_mutating(call.name)
            self.detector.record_action(
                call.name, call.arguments, mutating=is_mutating, succeeded=tool_result.ok
            )

            if is_mutating and tool_result.ok:
                mutated = True

            await self.emitter.tool_result(
                tool=call.name,
                call_id=call.id,
                ok=tool_result.ok,
                summary=tool_result.summary,
                duration_ms=tool_result.duration_ms,
                data=_result_data_for_event(tool_result),
                error_code=tool_result.error_code,
            )

            self.result.tool_calls.append(
                ToolCallRecord(
                    iteration=iteration,
                    tool=call.name,
                    arguments=call.arguments,
                    ok=tool_result.ok,
                    summary=tool_result.summary,
                    duration_ms=tool_result.duration_ms,
                    error_code=tool_result.error_code,
                )
            )
            self._absorb(call.name, call.arguments, tool_result, iteration)

            results.append(
                ToolCallResult(
                    id=call.id,
                    name=call.name,
                    content=tool_result.content,
                    is_error=not tool_result.ok,
                )
            )

            if self.tool_state.get("finish_requested"):
                # Answer the remaining calls so the transcript stays well-formed.
                for remaining in calls[calls.index(call) + 1 :]:
                    results.append(
                        ToolCallResult(
                            id=remaining.id,
                            name=remaining.name,
                            content="skipped: the run finished on the previous call",
                            is_error=False,
                        )
                    )
                break

        self.transcript.add_tool_results(results)
        return mutated

    def _is_mutating(self, tool_name: str) -> bool:
        try:
            return self.registry.get(tool_name).mutating
        except Exception:
            return False

    def _absorb(
        self, tool: str, arguments: dict[str, Any], result: ToolResult, iteration: int
    ) -> None:
        """Fold a tool result into the run record."""
        data = result.data or {}

        if tool == "run_tests" and result.ok:
            record = TestRunRecord(
                iteration=iteration,
                command=data.get("command", ""),
                passed=int(data.get("passed", 0)),
                failed=int(data.get("failed", 0)),
                errors=int(data.get("errors", 0)),
                skipped=int(data.get("skipped", 0)),
                total=int(data.get("total", 0)),
                green=bool(data.get("green", False)),
                duration_ms=int(data.get("duration_ms", 0)),
                framework=str(data.get("framework", "unknown")),
                timed_out=bool(data.get("timed_out", False)),
                failing_tests=list(data.get("failing_tests", [])),
                output=result.content[:60_000],
            )
            self.result.test_runs.append(record)
            self.result.metrics.test_runs += 1
            if not record.green:
                self._pending_directives.append(
                    failure_classification_hint(result.summary)
                )

        elif tool == "record_hypothesis" and result.ok:
            self.result.hypotheses.append(
                HypothesisRecord(
                    iteration=iteration,
                    index=int(data.get("index", len(self.result.hypotheses) + 1)),
                    suspected_cause=arguments.get("suspected_cause", ""),
                    evidence=arguments.get("evidence", ""),
                    files_involved=list(arguments.get("files_involved", [])),
                    proposed_fix=arguments.get("proposed_fix", ""),
                    validation_plan=arguments.get("validation_plan", ""),
                    supersedes_previous=bool(arguments.get("supersedes_previous", False)),
                )
            )
            self.result.metrics.hypotheses += 1

        elif tool in {"apply_patch", "edit_file", "write_file"}:
            if tool == "apply_patch":
                self.detector.record_patch(arguments.get("diff", ""), applied=result.ok)
            if result.ok:
                fallback = [arguments["path"]] if "path" in arguments else []
                for path in data.get("files", []) or fallback:
                    self._files_modified.add(str(path))

    def _last_test_summary(self) -> str | None:
        record = self.result.last_test_run
        if record is None:
            return None
        if record.green:
            return f"{record.passed} passed, 0 failed"
        return f"{record.passed} passed, {record.failed} failed, {record.errors} errors"

    # ------------------------------------------------------------- recovery
    async def _check_for_loops(self) -> None:
        signal = self.detector.check()
        if signal is None:
            return
        self.result.metrics.loop_signals += 1
        await self._set_state(RunState.REFLECTING)
        await self.emitter.loop_detected(kind=signal.kind, detail=signal.detail)
        await self.emitter.recovery_triggered(kind=signal.kind, directive=signal.directive)
        self._pending_directives.append(
            recovery_message(signal.directive, learned=self._what_we_know())
        )

    def _what_we_know(self) -> str:
        """A factual digest of the run so far, injected during recovery."""
        lines: list[str] = []
        inspected = self.workspace.files_read
        if inspected:
            lines.append(f"- Files inspected: {', '.join(inspected[:12])}")
        if self._files_modified:
            lines.append(f"- Files modified: {', '.join(sorted(self._files_modified))}")
        if self.result.hypotheses:
            current = self.result.hypotheses[-1]
            lines.append(f"- Current hypothesis: {current.suspected_cause}")
        if self.result.test_runs:
            last = self.result.test_runs[-1]
            lines.append(
                f"- Last test run `{last.command}`: {last.passed} passed, "
                f"{last.failed} failed, {last.errors} errors"
            )
            if last.failing_tests:
                lines.append(f"- Failing tests: {', '.join(last.failing_tests[:6])}")
        searches = [
            record.arguments.get("query")
            for record in self.result.tool_calls
            if record.tool == "search_code" and record.arguments.get("query")
        ]
        if searches:
            unique = list(dict.fromkeys(searches))[:8]
            lines.append(f"- Queries already tried: {', '.join(map(str, unique))}")
        return "\n".join(lines) or "- Nothing conclusive yet."

    def _inject_pending_directives(self) -> None:
        while self._pending_directives:
            self.transcript.add_user(self._pending_directives.pop(0))

    def _maybe_warn_about_budget(self) -> None:
        remaining = self.budget.budget.max_iterations - self.budget.iterations
        if remaining == 3:
            self.transcript.add_user(
                budget_warning_message("iteration", self.budget.describe_remaining())
            )

    # ------------------------------------------------------------ completion
    async def _handle_finish(self) -> None:
        resolved = bool(self.tool_state.get("finish_resolved"))
        summary = str(self.tool_state.get("finish_summary") or "")
        raw_class = self.tool_state.get("finish_failure_class")
        if raw_class:
            try:
                self.result.failure_class = FailureClass(raw_class)
            except ValueError:
                self.result.failure_class = FailureClass.UNKNOWN

        state = RunState.RESOLVED if resolved else RunState.FAILED
        await self._finalize(state, summary or ("agent reported success" if resolved else
                                                "agent reported it could not fix the issue"))

    async def _finalize(self, state: RunState, stop_reason: str) -> None:
        """Capture the final patch, metrics and terminal event exactly once."""
        if self.result.state.is_terminal:
            return

        try:
            self.result.final_patch = await self.workspace.diff()
            stat = await self.workspace.diff_stat()
            self._files_modified.update(stat.files)
        except Exception as exc:
            log.warning("agent.final_diff_failed", error=str(exc))
            stat = None

        # A run that claims RESOLVED with no patch is not resolved.
        if state is RunState.RESOLVED and not self.result.final_patch.strip():
            state = RunState.FAILED
            stop_reason = "agent reported success but produced no patch"

        self.result.state = state
        self.result.stop_reason = stop_reason
        self.result.summary = str(self.tool_state.get("finish_summary") or stop_reason)
        self.result.files_inspected = self.workspace.files_read
        self.result.files_modified = sorted(self._files_modified)

        usage = self.provider.usage()
        self.result.metrics = RunMetrics(
            iterations=self.budget.iterations,
            tool_calls=self.budget.tool_calls,
            model_calls=self.result.metrics.model_calls,
            files_inspected=len(self.result.files_inspected),
            files_modified=len(self.result.files_modified),
            test_runs=self.result.metrics.test_runs,
            hypotheses=self.result.metrics.hypotheses,
            patch_attempts=len(self.result.patch_attempts),
            runtime_s=self.budget.elapsed_s,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cache_read_tokens,
            cost_usd=usage.cost_usd,
            loop_signals=self.result.metrics.loop_signals,
        )

        if stat is not None and self.result.final_patch.strip():
            self.result.patch_attempts.append(
                PatchAttemptRecord(
                    iteration=self.budget.iterations,
                    diff=self.result.final_patch,
                    files=stat.files,
                    insertions=stat.insertions,
                    deletions=stat.deletions,
                    applied=True,
                )
            )
            self.result.metrics.patch_attempts = len(self.result.patch_attempts)

        await self._set_state(state)
        payload = {
            "stop_reason": stop_reason,
            "summary": self.result.summary,
            "metrics": self.result.metrics.to_dict(),
            "files_modified": self.result.files_modified,
            "has_patch": bool(self.result.final_patch.strip()),
        }
        if state is RunState.RESOLVED:
            await self.emitter.run_resolved(**payload)
        elif state is RunState.CANCELLED:
            await self.emitter.run_cancelled(**payload)
        else:
            await self.emitter.run_failed(error=self.result.error, **payload)


def _result_data_for_event(result: ToolResult) -> dict[str, Any]:
    """Trim tool result data before it becomes an event payload."""
    data = dict(result.data or {})
    data.pop("diff", None)  # patches are emitted by PATCH_APPLIED, not twice
    for key in ("entries", "files", "failing_tests", "commits"):
        value = data.get(key)
        if isinstance(value, list) and len(value) > 50:
            data[key] = [*value[:50], f"...[{len(value) - 50} more]"]
    return data
