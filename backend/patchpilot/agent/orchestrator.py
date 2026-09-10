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
import contextlib
import json
import re
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
from ..repair.patchquality import assess_patch
from ..repair.progress import Pressure, ProgressTracker
from ..sandbox.base import Sandbox
from ..tools.base import ToolContext, ToolResult
from ..tools.registry import ToolRegistry
from ..workspace.workspace import Workspace
from .antiloop import LoopDetector
from .budget import Budget, BudgetTracker
from .environment import RuntimeEnvironment
from .environment import apply_to_sandbox as apply_environment
from .environment import probe as probe_environment
from .prompts import (
    budget_warning_message,
    build_system_prompt,
    continuation_message,
    failure_classification_hint,
    final_turn_message,
    initial_user_message,
    low_information_message,
    no_patch_yet_message,
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

#: Directories that never contain the project's own test suite.
_SKIP_DIRS = frozenset(
    {"node_modules", "venv", ".venv", "dist", "build", "docs", "examples",
     "vendor", "third_party", "site-packages", "target", "bin", "obj"}
)


#: `npm init` writes a `test` script that always fails. Trusting it makes the
#: agent report "Error: no test specified" instead of running the suite the
#: repository actually has -- a very common state for young projects.
_NPM_PLACEHOLDER_TEST = re.compile(r"no test specified", re.IGNORECASE)

#: JS test runners, in the order we prefer them when the npm script is unusable.
_JS_RUNNERS: tuple[tuple[str, str], ...] = (
    ("vitest", "npx vitest run"),
    ("jest", "npx jest"),
    ("mocha", "npx mocha"),
    ("ava", "npx ava"),
)


#: Test runners a project ships itself, tried when pytest is unavailable.
#: These are conventions, not project-specific rules: sympy drives its suite
#: with `bin/test`, Django with `tests/runtests.py`, and many older projects
#: with a top-level `runtests.py`.
_NATIVE_RUNNERS: tuple[tuple[str, str], ...] = (
    ("bin/test", "python bin/test"),
    ("tests/runtests.py", "python tests/runtests.py"),
    ("runtests.py", "python runtests.py"),
)


def _test_command_for(directory: Path, *, has_pytest: bool = True) -> str | None:
    """The test command a directory's layout implies, or None.

    ``has_pytest`` comes from the environment probe. Without it this returned
    `python -m pytest -q` for any tree containing a `conftest.py` -- which is
    true of sympy, whose interpreter has no pytest at all. Every task in the
    first benchmark batch was handed that command, and it exits 1 with no
    output, which the agent reasonably read as a broken repository.
    """
    if (directory / "package.json").exists():
        try:
            manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
        script = scripts.get("test", "")
        if script and not _NPM_PLACEHOLDER_TEST.search(str(script)):
            return "npm test --silent"

        # The script is missing or is the placeholder: infer the runner from
        # the declared dependencies instead.
        declared = {
            *(manifest.get("devDependencies") or {}),
            *(manifest.get("dependencies") or {}),
        }
        for package, command in _JS_RUNNERS:
            if package in declared:
                return command
        return "npx jest"

    if not has_pytest:
        # Never suggest a runner the interpreter cannot load.
        for marker, command in _NATIVE_RUNNERS:
            if (directory / marker).exists():
                return command
        for tests in ("tests", "test"):
            candidate = directory / tests
            if candidate.is_dir() and any(candidate.glob("test_*.py")):
                return "python -m unittest discover"
        return None

    for marker in ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py"):
        if (directory / marker).exists():
            return "python -m pytest -q"
    for tests in ("tests", "test"):
        candidate = directory / tests
        if candidate.is_dir() and any(candidate.glob("test_*.py")):
            return "python -m pytest -q"
    if (directory / "go.mod").exists():
        return "go test ./..."
    if (directory / "Cargo.toml").exists():
        return "cargo test"
    return None


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
    seed_patch: str | None = None
    """A diff to apply to the fresh workspace before the agent starts.

    Used when a reviewer asks for changes: the run re-prepares from the same
    base commit, then the previously reviewed patch is restored, so the agent
    refines that solution instead of rediscovering it.
    """
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
        self._wrapup_used = False
        self._pending_directives: list[str] = []
        self._interventions: list[str] = []
        """Every steer the loop applied, in order, for the run record."""
        self.environment = RuntimeEnvironment()
        self.progress = ProgressTracker()
        self.iteration_extension_fraction = 0.25
        """Size of the one-off extension, as a fraction of the configured budget."""

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
        # Pin the revision this run is actually working from. Without it a
        # patch cannot be published later: there is no known base to apply it
        # to, only "whatever the branch happened to be".
        # The *resolved* head, not what the caller typed. A caller may pin an
        # abbreviated SHA -- "86112e6" is a perfectly good thing to paste into a
        # form -- but `git fetch <remote> <sha>` needs the full 40 characters,
        # and comparing an abbreviation against a full head reports drift that
        # is not there. `info.head` is the same commit, unambiguously.
        self.result.base_commit = info.head or self.task.base_commit
        self.result.base_branch = info.branch
        await self.emitter.repository_ready(
            repository=self.task.repository,
            head=info.head,
            branch=info.branch,
            base_commit=self.task.base_commit,
            sandbox=self.sandbox.info().kind,
            isolation=self.sandbox.info().isolation,
        )

        if self.task.seed_patch:
            await self._restore_previous_patch(self.task.seed_patch)

        # Somewhere to put reproduction scripts. `_stage_all` already excludes
        # `.patchpilot*` from the diff, but with nowhere obvious to write, one
        # run put its repro script in the repository root and submitted it as
        # the entire patch.
        with contextlib.suppress(OSError):
            (self.workspace.root / ".patchpilot").mkdir(exist_ok=True)

        # Probe the interpreter before anything else. 39% of the tool calls in
        # the first SWE-bench batch were spent rediscovering it per task; doing
        # it once here, off-budget, is the single largest saving available.
        self.environment = await probe_environment(self.sandbox, self.workspace.root)
        await apply_environment(self.sandbox, self.environment)
        if not self.environment.usable:
            await self.emitter.warning(
                "no working interpreter found; commands may fail for environment reasons",
                **self.environment.summary(),
            )

        if self.task.test_command is None:
            discovered, cwd = await self._discover_test_command()
            self.tool_state["test_command"] = discovered
            self.tool_state["test_cwd"] = cwd
            await self.emitter.warning(
                "no test command supplied; using a discovered default",
                test_command=discovered,
                test_cwd=cwd,
            )
        elif self.environment.probed and not self.environment.has_pytest:
            # A supplied command is usually `python -m pytest`, which cannot work
            # when the project's interpreter has no pytest. Fall back to the
            # project's own runner rather than handing over a command we already
            # know fails -- that lie cost whole trajectories.
            discovered, cwd = await self._discover_test_command()
            if discovered != self.task.test_command:
                self.tool_state["test_command"] = discovered
                self.tool_state["test_cwd"] = cwd
                await self.emitter.warning(
                    "supplied test command needs pytest, which this interpreter lacks",
                    supplied=self.task.test_command,
                    test_command=discovered,
                )

        await self._set_state(RunState.ANALYZING)
        await self.emitter.issue_analyzed(
            issue_length=len(self.task.issue),
            tools_available=self.registry.allowed_names,
            tools_blocked=self.registry.blocked_names,
            test_command=self.tool_state.get("test_command"),
            test_cwd=self.tool_state.get("test_cwd"),
        )

        self.transcript.add_user(initial_user_message(self.budget.describe_remaining()))

    async def _restore_previous_patch(self, diff: str) -> None:
        """Re-apply the patch a reviewer sent back, before the agent starts.

        Failure is not fatal: the workspace is simply clean and the agent works
        from the issue plus the feedback. Aborting the run would be worse -- the
        reviewer asked for a revision, not for nothing to happen.
        """
        patch_file = self.workspace.root / ".patchpilot" / "previous.patch"
        try:
            patch_file.parent.mkdir(parents=True, exist_ok=True)
            patch_file.write_text(diff, encoding="utf-8", newline="\n")
            result = await self.workspace.git(
                "apply", "--whitespace=nowarn", self.workspace.relpath(patch_file)
            )
        except Exception as exc:
            await self.emitter.warning(
                "could not restore the previously reviewed patch", error=str(exc)[:200]
            )
            return
        finally:
            with contextlib.suppress(OSError):
                patch_file.unlink(missing_ok=True)

        if result.ok:
            await self.emitter.warning(
                "restored the previously reviewed patch for revision",
                bytes=len(diff),
            )
        else:
            await self.emitter.warning(
                "the previously reviewed patch no longer applies; starting from a clean tree",
                detail=result.stderr[:200],
            )

    async def _discover_test_command(self) -> tuple[str, str | None]:
        """Guess a test command, and the directory to run it from.

        Returns ``(command, cwd)``. The cwd matters: in a monorepo the suite
        often lives in a subdirectory, and since commands never go through a
        shell there is no `cd x && ...` escape hatch — the agent would be
        structurally unable to run the tests without this.
        """
        root = self.workspace.root

        # Search the root first, then one level down. Deeper than that and the
        # guess is more likely to be wrong than useful.
        candidates: list[Path] = [root]
        with contextlib.suppress(OSError):
            candidates += sorted(
                d for d in root.iterdir()
                if d.is_dir() and not d.name.startswith((".", "_")) and d.name not in _SKIP_DIRS
            )

        # Only a *successful* probe can tell us pytest is absent. If it could not
        # find an interpreter at all it knows nothing, and assuming "no pytest"
        # would steer every ordinary Python project away from its real runner.
        has_pytest = self.environment.has_pytest or not self.environment.usable
        for directory in candidates:
            command = _test_command_for(directory, has_pytest=has_pytest)
            if command is None:
                continue
            relative = None if directory == root else self.workspace.relpath(directory)
            return command, relative

        # Last resort. When the probe told us there is no pytest, saying so is
        # more useful than naming a command that cannot run.
        return ("python -m pytest -q" if has_pytest else "python -m unittest discover"), None

    # ------------------------------------------------------------- main loop
    async def _loop(self) -> None:
        system_prompt = build_system_prompt(
            repository=self.task.repository,
            issue=self.task.issue,
            test_command=self.tool_state.get("test_command"),
            base_commit=self.task.base_commit,
            hints=self.task.hints,
            extra_guidance=self.task.extra_guidance,
            environment=self.environment.describe(),
        )
        tool_specs = [spec.to_dict() for spec in self.registry.specs()]

        while True:
            if self._cancelled:
                await self._finalize(RunState.CANCELLED, "cancelled by operator")
                return

            limit = self.budget.exceeded()
            if limit == "max_iterations":
                extra = self._extension_iterations()
                if extra and self.budget.grant_iteration_extension(extra):
                    await self.emitter.warning(
                        "granting extra iterations to a run that is still converging",
                        extra_iterations=extra,
                        snapshot=self.budget.snapshot(),
                    )
                    limit = self.budget.exceeded()
            if limit:
                await self.emitter.budget_exceeded(limit=limit, snapshot=self.budget.snapshot())
                # The budget bounds how much *work* the agent may do, not its
                # ability to report. Without this, an agent that spends its last
                # iteration on a final validation run — exactly the right thing
                # to do — has a complete, green patch discarded as TIMEOUT.
                if await self._final_turn(system_prompt, limit):
                    return
                await self._finalize(
                    RunState.TIMEOUT, f"stopped after reaching the {limit} limit"
                )
                return

            self.budget.start_iteration()
            iteration = self.budget.iterations
            self.emitter.set_iteration(iteration)
            await self.emitter.iteration_started(iteration, self.budget.snapshot())

            self._publish_repair_state()
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

            files_before = len(self.progress.files_seen)
            mutated = await self._execute_tool_calls(response.tool_calls, iteration)

            if self.tool_state.get("finish_requested"):
                await self._handle_finish()
                return

            self.detector.end_iteration(mutated=mutated)
            self.progress.end_iteration(
                mutated=mutated, saw_new_file=len(self.progress.files_seen) > files_before
            )
            await self._check_for_loops()
            await self._maybe_push_toward_a_patch()

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

    async def _final_turn(self, system_prompt: str, limit: str) -> bool:
        """Give the agent one last turn, to conclude rather than to work.

        Only ``finish`` is offered, so the turn cannot be spent on more
        investigation. Returns True if the run reached a terminal state.
        """
        if self._wrapup_used:
            return False
        self._wrapup_used = True

        try:
            finish_tool = self.registry.get("finish").spec().to_dict()
        except Exception:
            return False

        await self._set_state(RunState.REFLECTING)
        self.transcript.add_user(final_turn_message(limit, self._what_we_know()))

        response = await self._call_model(system_prompt, [finish_tool])
        if response is None:
            return True  # _call_model already finalised the run

        await self.emitter.agent_message(response.text)
        self.transcript.add_assistant(response.text, response.tool_calls)
        if not response.tool_calls:
            return False

        await self._execute_tool_calls(response.tool_calls, self.budget.iterations)
        if self.tool_state.get("finish_requested"):
            await self._handle_finish()
            return True
        return False

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
            self.progress.record_tool(
                call.name, call.arguments, iteration, succeeded=tool_result.ok
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
                summary_recognized=bool(data.get("summary_recognized", False)),
                xfail=int(data.get("xfail", 0)),
                failing_tests=list(data.get("failing_tests", [])),
                output=result.content[:60_000],
            )
            self.result.test_runs.append(record)
            self.result.metrics.test_runs += 1
            self.progress.record_test(iteration, green=record.green)
            # Verification is only meaningful after the code changed. A suite
            # that was green before the edit says nothing about the fix, and a
            # later edit invalidates an earlier pass.
            #
            # It also requires that we actually understood the runner. A command
            # that exits 0 on output we could not parse is not evidence: for a
            # whole benchmark family the parser did not know SymPy's runner, and
            # every "verified" reading meant nothing more than exit code 0.
            if record.green and record.summary_recognized and self._files_modified:
                self.result.metrics.verified_by_tests = True
                self.tool_state["tests_stale"] = False
            if not record.green:
                self.result.metrics.verified_by_tests = False
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
                # Editing after a green run means that green no longer covers
                # the working tree; the agent must re-run to claim success.
                self.result.metrics.verified_by_tests = False
                self.tool_state["tests_stale"] = True

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

    def _extension_iterations(self) -> int:
        """How many extra iterations this run has earned, if any.

        Deliberately narrow. Extra budget only helps a run that already has a
        patch and is learning from test results -- the shape of the two
        instances the harness resolved while PatchPilot had recorded them as
        TIMEOUT and FAILED. A run with no patch, or one whose tests already pass,
        or one that has stopped testing, gains nothing from more turns, and this
        must not become a way of papering over the paralysis the rest of these
        changes address.
        """
        if not self.progress.has_source_edit:
            return 0  # nothing to protect; more turns would be more exploration
        if self.result.metrics.verified_by_tests:
            return 0  # already green; the wrap-up turn is enough to report it
        if not (self.result.test_runs or self.progress.behaviour_checked):
            # A run that has never checked its own change is not converging on
            # anything; extra turns reward the trajectory we are trying to end.
            return 0
        last_checked_at = max(
            [r.iteration for r in self.result.test_runs]
            + ([self.progress.first_behaviour_check]
               if self.progress.first_behaviour_check else [])
        )
        if self.budget.iterations - last_checked_at > 3:
            return 0  # no longer in the patch-test-observe cycle
        base = self.budget.budget.max_iterations
        return max(1, round(base * self.iteration_extension_fraction))

    def _publish_repair_state(self) -> None:
        """Expose repair state to the tools, so `finish` can reason about it.

        One source of truth: the tracker owns the state, the tools read it. The
        alternative -- tools recomputing progress from the transcript -- is how
        two components come to disagree about whether a run has a diagnosis.
        """
        budget = self.budget.budget.max_iterations or 1
        self.tool_state["repair_has_diagnosis"] = self.progress.has_diagnosis
        self.tool_state["repair_candidate_file"] = self.progress.best_candidate()
        self.tool_state["budget_fraction_remaining"] = max(
            0.0, (budget - self.budget.iterations) / budget
        )

    async def _maybe_push_toward_a_patch(self) -> None:
        """Escalate toward a *source* edit when a run is all investigation.

        Two failures live here, and they need different words.

        *Paralysis*: six of fifteen runs reached the iteration cap without
        editing. The anti-loop detector said nothing, because none of them was
        repeating an action -- each turn explored something new. All six named
        the right file in their final summary and never opened an editor on it.

        *Reproduction capture*: the second pass found the mechanism. Every one of
        those runs built a reproduction before editing (up to ten times); every
        run that produced a patch reproduced only afterwards. Rebuilding the
        reproduction feels like progress and is not.

        This advises; it never edits. Forcing a change at a fixed iteration would
        trade six no-patch runs for six random-patch runs.
        """
        if self._pending_directives:
            # The loop detector already has something to say this iteration.
            # Two long directives at once buries both.
            return

        pressure = self.progress.pressure(
            self.budget.iterations, self.budget.budget.max_iterations
        )
        if pressure is not Pressure.NONE:
            key = f"pressure:{pressure}"
            if self.progress.should_warn(key):
                directive = no_patch_yet_message(
                    pressure=str(pressure),
                    budget_line=self.budget.describe_remaining(),
                    candidate=self.progress.best_candidate(),
                    learned=self._what_we_know(),
                    reproductions=self.progress.reproductions_before_edit,
                )
                self._pending_directives.append(directive)
                self._interventions.append(key)
                await self.emitter.recovery_triggered(kind=key, directive=directive)
                return

        # Re-reading something that has already been read to exhaustion cannot
        # change the repair. Successful runs peaked at 1-2 looks per target;
        # the empty-patch runs reached 5-7.
        target = self.progress.over_inspected()
        if target and self.progress.should_warn(f"reinspect:{target}"):
            directive = low_information_message(
                target=target,
                has_patch=self.progress.has_source_edit,
                learned=self._what_we_know(),
            )
            self._pending_directives.append(directive)
            self._interventions.append(f"reinspect:{target}")
            await self.emitter.recovery_triggered(
                kind="low_information", directive=directive
            )

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

        # The agent's belief and the evidence for it are recorded separately.
        # In the first benchmark batch the agent called 5 instances RESOLVED and
        # the harness confirmed 2 -- while finding 2 more among the ones it had
        # given up on. Its opinion is a signal, not a verdict, so we keep the
        # claim and note whether a `run_tests` call ever actually went green
        # against the current working tree.
        self.result.agent_claimed_resolved = resolved
        if resolved and not self.result.metrics.verified_by_tests:
            await self.emitter.warning(
                "agent reported success without an observed green test run",
                verified_by_tests=False,
                test_runs=self.result.metrics.test_runs,
            )
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

        # What the diff *is*, judged from the diff rather than from the agent's
        # opinion of it. A run whose only changes are debug artefacts produced no
        # repair, however confidently it reported otherwise.
        self.result.patch_assessment = assess_patch(self.result.final_patch)

        # A run that claims RESOLVED without a real repair is not resolved.
        if state is RunState.RESOLVED and not self.result.patch_assessment.is_repair:
            state = RunState.FAILED
            stop_reason = (
                "agent reported success but the diff contains no repair: "
                f"{self.result.patch_assessment.describe()}"
            )

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
            # Progress diagnostics. This constructor replaces the metrics object
            # wholesale, so anything not carried across here is silently lost.
            time_to_first_edit=self.progress.first_source_edit,
            time_to_first_source_edit=self.progress.first_source_edit,
            time_to_first_any_write=self.progress.first_any_write,
            time_to_first_scratch_write=self.progress.first_scratch_write,
            time_to_first_test_edit=self.progress.first_test_edit,
            time_to_first_behaviour_check=self.progress.first_behaviour_check,
            time_to_first_file_read=self.progress.first_file_read,
            time_to_first_hypothesis=self.progress.first_hypothesis,
            time_to_first_test=self.progress.first_test_run,
            time_to_first_green_test=self.progress.first_green_test,
            verified_by_tests=self.result.metrics.verified_by_tests,
            reproductions_before_edit=self.progress.reproductions_before_edit,
            source_edits=self.progress.source_edits,
            scratch_writes=self.progress.scratch_writes,
            patch_quality=str(self.result.patch_assessment.quality),
            meaningful_patch=self.result.patch_assessment.is_repair,
            interventions=len(self._interventions),
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
