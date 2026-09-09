"""Agent loop tests.

These drive the real orchestrator, real tools, a real workspace and a real
sandbox — only the *model* is a scripted double. That is the point: everything
that makes the loop work (test feedback, patch validation, anti-loop recovery,
budgets, event ordering) is exercised for real, deterministically and for free.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from patchpilot.agent.orchestrator import AgentTask
from patchpilot.agent.session import SessionConfig, run_agent_task
from patchpilot.domain.enums import EventType, RunState
from patchpilot.events.bus import MemorySink
from patchpilot.fixtures import get_fixture, list_fixtures
from patchpilot.providers.scripted import ScriptedProvider, ScriptedTurn, last_tool_output

PY = sys.executable
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def make_task(fixture_id: str, run_id: str, **overrides) -> AgentTask:
    fixture = get_fixture(fixture_id)
    return AgentTask(
        run_id=run_id,
        issue=fixture.issue,
        repository=fixture.repository,
        local_path=fixture.repo_path,
        test_command=f"{PY} -m pytest tests/ -q",
        **overrides,
    )


# ------------------------------------------------------------------ fixtures


def test_every_fixture_declares_a_complete_task() -> None:
    fixtures = list_fixtures()
    assert {f.id for f in fixtures} == {"calculator_bug", "auth_token_bug", "pagination_bug"}
    for fixture in fixtures:
        assert fixture.repo_path.exists()
        assert len(fixture.issue) > 100, f"{fixture.id} needs a realistic issue"
        assert fixture.expected_failing_tests
        assert fixture.expected_files


# ---------------------------------------------------- the happy path, 1 pass


async def test_agent_fixes_the_calculator_bug(settings, tmp_path) -> None:
    """Investigate -> hypothesise -> patch -> test -> finish."""
    fix = (
        '    if b == 0:\n        return None\n    return a / b'
    )
    script = [
        ScriptedTurn(
            text="Searching for the divide implementation.",
            tool_calls=[("search_code", {"query": "def divide"})],
        ),
        ScriptedTurn(
            text="Reading the calculator module.",
            tool_calls=[("read_file", {"path": "src/calculator.py"})],
        ),
        ScriptedTurn(
            text="Recording my hypothesis.",
            tool_calls=[
                (
                    "record_hypothesis",
                    {
                        "suspected_cause": "divide() performs a / b with no zero guard",
                        "evidence": "src/calculator.py divide() returns a / b directly, "
                        "but its return type is float | None",
                        "files_involved": ["src/calculator.py"],
                        "proposed_fix": "return None when b == 0",
                        "validation_plan": "python -m pytest tests/ -q",
                    },
                )
            ],
        ),
        ScriptedTurn(
            text="Applying the guard.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    return a / b",
                        "new_string": fix,
                    },
                )
            ],
        ),
        ScriptedTurn(text="Running the tests.", tool_calls=[("run_tests", {})]),
        ScriptedTurn(
            text="All green.",
            tool_calls=[
                ("finish", {"resolved": True, "summary": "Guarded divide() against b == 0."})
            ],
        ),
    ]

    settings.workspace_root = tmp_path / "ws"
    sink = MemorySink()
    result = await run_agent_task(
        make_task("calculator_bug", "run-calc-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(script=script),
        sinks=[sink],
    )

    assert result.state is RunState.RESOLVED, result.stop_reason
    assert result.resolved
    assert "src/calculator.py" in result.files_modified
    assert "return None" in result.final_patch
    assert result.final_patch.startswith("diff --git")

    assert result.metrics.test_runs == 1
    assert result.metrics.hypotheses == 1
    assert result.metrics.iterations == 6

    last = result.last_test_run
    assert last is not None and last.green
    assert last.failed == 0 and last.passed == 10

    types = sink.types()
    for expected in (
        "RUN_STARTED" if "RUN_STARTED" in types else "REPOSITORY_READY",
        "ISSUE_ANALYZED",
        "TOOL_CALLED",
        "TOOL_RESULT",
        "HYPOTHESIS_CREATED",
        "TEST_STARTED",
        "TEST_FINISHED",
        "RUN_RESOLVED",
    ):
        assert expected in types, f"missing {expected} event"


# ----------------------------------------------- the real feature: iteration


async def test_agent_reacts_to_a_failing_patch_and_tries_again(settings, tmp_path) -> None:
    """First patch is wrong; the agent must observe the failure and fix it.

    This is the behaviour the whole project exists for, so it is asserted
    directly: two patches, two test runs, first red, second green.
    """
    settings.workspace_root = tmp_path / "ws"

    def responder(messages, index):
        if index == 0:
            return ScriptedTurn(
                text="Looking at the pagination module.",
                tool_calls=[("read_file", {"path": "src/pagination.py"})],
            )
        if index == 1:
            return ScriptedTurn(
                text="total_pages truncates; adding one.",
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "src/pagination.py",
                            # Naive fix: breaks the empty-list case.
                            "old_string": "        return self.total // self.per_page",
                            "new_string": "        return self.total // self.per_page + 1",
                        },
                    )
                ],
            )
        if index == 2:
            return ScriptedTurn(text="Running tests.", tool_calls=[("run_tests", {})])
        if index == 3:
            observation = last_tool_output(messages, "run_tests")
            # The agent must genuinely see the regression it caused.
            assert "failed" in observation, observation[:400]
            assert "test_empty_list" in observation
            return ScriptedTurn(
                text="I broke the empty case. Using a proper ceiling instead.",
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "src/pagination.py",
                            "old_string": "        return self.total // self.per_page + 1",
                            "new_string": "        return -(-self.total // self.per_page)",
                        },
                    )
                ],
            )
        if index == 4:
            return ScriptedTurn(text="Re-running tests.", tool_calls=[("run_tests", {})])
        return ScriptedTurn(
            text="Green.",
            tool_calls=[
                (
                    "finish",
                    {"resolved": True, "summary": "total_pages now uses a ceiling division."},
                )
            ],
        )

    result = await run_agent_task(
        make_task("pagination_bug", "run-page-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )

    assert result.state is RunState.RESOLVED, result.stop_reason
    assert len(result.test_runs) == 2
    assert result.test_runs[0].green is False, "first patch should have failed"
    assert result.test_runs[1].green is True, "second patch should have passed"
    assert result.test_runs[0].failed > 0
    assert "-(-self.total // self.per_page)" in result.final_patch


async def test_agent_iterates_across_two_files(settings, tmp_path) -> None:
    """Fixing only one endpoint leaves a test failing; the agent must notice."""
    settings.workspace_root = tmp_path / "ws"

    old_refresh = (
        "    try:\n"
        "        token = verify_refresh_token(store, refresh_token)\n"
        "    except InvalidTokenError as exc:\n"
        '        return _json(401, {"error": "invalid_token", "detail": str(exc)})'
    )
    new_refresh = (
        "    try:\n"
        "        token = verify_refresh_token(store, refresh_token)\n"
        "    except TokenError as exc:\n"
        '        return _json(exc.status_code, {"error": "invalid_token", "detail": str(exc)})'
    )
    old_logout = (
        "    try:\n"
        "        verify_refresh_token(store, refresh_token)\n"
        "    except InvalidTokenError as exc:\n"
        '        return _json(401, {"error": "invalid_token", "detail": str(exc)})'
    )
    new_logout = (
        "    try:\n"
        "        verify_refresh_token(store, refresh_token)\n"
        "    except TokenError as exc:\n"
        '        return _json(exc.status_code, {"error": "invalid_token", "detail": str(exc)})'
    )

    def responder(messages, index):
        if index == 0:
            return ScriptedTurn(
                text="Finding the token error classes.",
                tool_calls=[("search_code", {"query": "ExpiredTokenError"})],
            )
        if index == 1:
            return ScriptedTurn(
                text="Reading the HTTP layer.",
                tool_calls=[("read_file", {"path": "src/auth/api.py"})],
            )
        if index == 2:
            return ScriptedTurn(
                text="Widening the except clause in refresh_session, and importing TokenError.",
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "src/auth/api.py",
                            "old_string": "from .token import InvalidTokenError, TokenStore, verify_refresh_token",
                            "new_string": "from .token import TokenError, TokenStore, verify_refresh_token",
                        },
                    ),
                    (
                        "edit_file",
                        {
                            "path": "src/auth/api.py",
                            "old_string": old_refresh,
                            "new_string": new_refresh,
                        },
                    ),
                ],
            )
        if index == 3:
            return ScriptedTurn(text="Testing.", tool_calls=[("run_tests", {})])
        if index == 4:
            observation = last_tool_output(messages, "run_tests")
            assert "test_expired_token_on_logout_is_401_not_500" in observation
            return ScriptedTurn(
                text="logout has the same bug; fixing it too.",
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "src/auth/api.py",
                            "old_string": old_logout,
                            "new_string": new_logout,
                        },
                    )
                ],
            )
        if index == 5:
            return ScriptedTurn(text="Re-testing.", tool_calls=[("run_tests", {})])
        return ScriptedTurn(
            text="Done.",
            tool_calls=[
                (
                    "finish",
                    {
                        "resolved": True,
                        "summary": "Both endpoints now map TokenError to its status_code.",
                    },
                )
            ],
        )

    result = await run_agent_task(
        make_task("auth_token_bug", "run-auth-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )

    assert result.state is RunState.RESOLVED, result.stop_reason
    assert len(result.test_runs) == 2
    assert not result.test_runs[0].green
    assert result.test_runs[1].green
    assert result.test_runs[1].passed == 9


# --------------------------------------------------------------- guardrails


async def test_finish_is_refused_when_tests_are_red(settings, tmp_path) -> None:
    """The agent cannot declare success it has not observed."""
    settings.workspace_root = tmp_path / "ws"
    calls: list[str] = []

    def responder(messages, index):
        if index == 0:
            return ScriptedTurn(
                text="Breaking something on purpose.",
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "src/calculator.py",
                            "old_string": "    return a + b",
                            "new_string": "    return a + b + 1",
                        },
                    )
                ],
            )
        if index == 1:
            return ScriptedTurn(text="Testing.", tool_calls=[("run_tests", {})])
        if index == 2:
            return ScriptedTurn(
                text="Claiming success anyway.",
                tool_calls=[("finish", {"resolved": True, "summary": "done"})],
            )
        calls.append(last_tool_output(messages, "finish"))
        return ScriptedTurn(
            text="Fine, reporting honestly.",
            tool_calls=[
                ("finish", {"resolved": False, "summary": "could not fix", "failure_class": "REGRESSION"})
            ],
        )

    result = await run_agent_task(
        make_task("calculator_bug", "run-guard-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )

    assert calls, "the premature finish should have been rejected and observed"
    assert "not green" in calls[0]
    assert result.state is RunState.FAILED
    assert str(result.failure_class) == "REGRESSION"


async def test_finish_is_refused_when_nothing_was_changed(settings, tmp_path) -> None:
    """Claiming success without touching the code is rejected outright."""
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 4
    refusals: list[str] = []

    def responder(messages, index):
        if index == 0:
            return ScriptedTurn(
                text="Claiming success with no changes.",
                tool_calls=[("finish", {"resolved": True, "summary": "nothing to do"})],
            )
        refusals.append(last_tool_output(messages, "finish"))
        return ScriptedTurn(
            text="Reporting honestly.",
            tool_calls=[("finish", {"resolved": False, "summary": "no fix found"})],
        )

    result = await run_agent_task(
        make_task("calculator_bug", "run-guard-2"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )
    assert refusals, "the no-patch finish should have been refused"
    assert "no changes" in refusals[0]
    assert result.state is RunState.FAILED
    assert not result.final_patch.strip()


# ------------------------------------------------------------------ budgets


async def test_iteration_budget_stops_the_run(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 3

    def responder(messages, index):
        return ScriptedTurn(
            text="Looking around.", tool_calls=[("list_files", {"path": ".", "depth": 1})]
        )

    sink = MemorySink()
    result = await run_agent_task(
        make_task("calculator_bug", "run-budget-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
        sinks=[sink],
    )

    assert result.state is RunState.TIMEOUT
    assert result.metrics.iterations == 3
    assert "max_iterations" in result.stop_reason
    assert EventType.BUDGET_EXCEEDED in [e.type for e in sink.events]


async def test_tool_call_budget_stops_the_run(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 50
    settings.max_tool_calls = 4

    def responder(messages, index):
        return ScriptedTurn(
            text="Searching.", tool_calls=[("search_code", {"query": f"term{index}"})]
        )

    result = await run_agent_task(
        make_task("calculator_bug", "run-budget-2"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )
    assert result.state is RunState.TIMEOUT
    assert "max_tool_calls" in result.stop_reason
    assert result.metrics.tool_calls >= 4


async def test_per_run_budget_override(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 50

    def responder(messages, index):
        return ScriptedTurn(text="x", tool_calls=[("list_files", {})])

    result = await run_agent_task(
        make_task("calculator_bug", "run-budget-3", budget_overrides={"max_iterations": 2}),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )
    assert result.metrics.iterations == 2


# ------------------------------------------------------------ anti-loop path


async def test_repetition_triggers_a_recovery_directive(settings, tmp_path) -> None:
    """Reading the same file over and over must provoke an injected re-plan."""
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 8
    seen_directive: list[str] = []

    def responder(messages, index):
        for message in messages:
            if (
                message.role == "user"
                and "STOP AND RE-PLAN" in message.content
                and message.content not in seen_directive
            ):
                seen_directive.append(message.content)
        return ScriptedTurn(
            text="Reading the same file again.",
            tool_calls=[("read_file", {"path": "src/calculator.py"})],
        )

    sink = MemorySink()
    result = await run_agent_task(
        make_task("calculator_bug", "run-loop-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
        sinks=[sink],
    )

    assert seen_directive, "the loop detector should have injected a recovery directive"
    directive = seen_directive[0]
    assert "read_file" in directive
    assert "different" in directive.lower()
    assert "What this run has established so far" in directive

    kinds = [e.payload.get("kind") for e in sink.of_type(EventType.LOOP_DETECTED)]
    assert any(k and k.startswith("repeated_action") for k in kinds)
    assert result.metrics.loop_signals >= 1


async def test_stall_detection_when_nothing_is_edited(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 10

    def responder(messages, index):
        # Distinct actions each turn, so only the *stall* rule can fire.
        return ScriptedTurn(
            text="Searching.", tool_calls=[("search_code", {"query": f"unique_term_{index}"})]
        )

    sink = MemorySink()
    await run_agent_task(
        make_task("calculator_bug", "run-loop-2"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
        sinks=[sink],
    )

    kinds = [e.payload.get("kind") for e in sink.of_type(EventType.LOOP_DETECTED)]
    assert "stalled" in kinds


# ---------------------------------------------------------- error resilience


async def test_a_run_with_a_bad_repository_fails_cleanly(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    task = AgentTask(
        run_id="run-bad-1",
        issue="something is broken",
        repository="nope",
        local_path=tmp_path / "does-not-exist",
    )
    result = await run_agent_task(
        task,
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(script=[ScriptedTurn(text="hi")]),
    )
    assert result.state is RunState.FAILED
    assert result.error is not None
    assert "local repository not found" in result.error


async def test_a_turn_with_no_tool_call_is_nudged(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 4
    nudged: list[bool] = []

    def responder(messages, index):
        if index > 0:
            nudged.append(
                any("did not call a tool" in m.content for m in messages if m.role == "user")
            )
        return ScriptedTurn(text="Just thinking out loud.", tool_calls=[])

    await run_agent_task(
        make_task("calculator_bug", "run-notool-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )
    assert any(nudged)


async def test_workspace_is_cleaned_up_by_default(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    await run_agent_task(
        make_task("calculator_bug", "run-cleanup-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(
            script=[ScriptedTurn(tool_calls=[("finish", {"resolved": False, "summary": "bye"})])]
        ),
    )
    assert not (settings.workspace_root / "run-cleanup-1").exists()


async def test_events_carry_a_monotonic_sequence(settings, tmp_path) -> None:
    settings.workspace_root = tmp_path / "ws"
    sink = MemorySink()
    await run_agent_task(
        make_task("calculator_bug", "run-seq-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(
            script=[
                ScriptedTurn(tool_calls=[("list_files", {})]),
                ScriptedTurn(tool_calls=[("finish", {"resolved": False, "summary": "bye"})]),
            ]
        ),
        sinks=[sink],
    )
    seqs = [e.seq for e in sink.events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    assert seqs[0] == 1


@pytest.mark.parametrize("fixture_id", ["calculator_bug", "auth_token_bug", "pagination_bug"])
async def test_agent_can_run_every_fixture_without_crashing(
    settings, tmp_path, fixture_id: str
) -> None:
    """A smoke test over all fixtures: real repo, real tools, real test runs."""
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 4

    def responder(messages, index):
        if index == 0:
            return ScriptedTurn(tool_calls=[("list_files", {"path": ".", "depth": 2})])
        if index == 1:
            return ScriptedTurn(tool_calls=[("run_tests", {})])
        return ScriptedTurn(
            tool_calls=[("finish", {"resolved": False, "summary": "reconnaissance only"})]
        )

    result = await run_agent_task(
        make_task(fixture_id, f"run-smoke-{fixture_id}"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )
    assert result.state is RunState.FAILED  # finished honestly, did not resolve
    assert result.metrics.test_runs == 1
    assert result.test_runs[0].failed > 0, "the fixture bug should make tests fail"


async def test_refused_commands_do_not_count_as_test_runs(settings, tmp_path) -> None:
    """A refused call observed nothing, so calling it a test run is a lie.

    The detector used to count every run_command — including ones the guard
    rejected — as test churn, then told the agent "you have run tests 3 times
    without changing any code" when it had run none.
    """
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 6
    directives: list[str] = []

    def responder(messages, index):
        for message in messages:
            if message.role == "user" and "run tests" in message.content:
                directives.append(message.content)
        # Always refused by the guard: a bare shell operator.
        return ScriptedTurn(
            text="probing", tool_calls=[("run_command", {"command": ["python", "&&"]})]
        )

    await run_agent_task(
        make_task("calculator_bug", "run-refused-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
    )
    assert not directives, f"refused calls were counted as test runs: {directives[:1]}"


async def test_find_symbol_tolerates_a_def_prefix(registry, tool_ctx) -> None:
    """Models pass 'def divide' as often as 'divide'."""
    plain = await registry.call("find_symbol", tool_ctx, {"symbol": "divide"})
    prefixed = await registry.call("find_symbol", tool_ctx, {"symbol": "def divide"})
    assert plain.ok and prefixed.ok
    assert prefixed.data["definition_count"] == plain.data["definition_count"] >= 1
    assert prefixed.data["symbol"] == "divide"


async def test_a_green_run_that_uses_its_whole_budget_can_still_finish(
    settings, tmp_path
) -> None:
    """Spending the last iteration on a final validation must not lose the work.

    A real run fixed a bug correctly, ran the full suite green on its final
    iteration, and was recorded as TIMEOUT / NOT RESOLVED because the budget
    check fired before it could call finish. The budget bounds how much *work*
    the agent may do, not its ability to report a verdict.
    """
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 3
    offered: list[list[str]] = []

    provider = ScriptedProvider(
        responder=lambda messages, index: (
            ScriptedTurn(
                tool_calls=[
                    (
                        "edit_file",
                        {
                            "path": "src/calculator.py",
                            "old_string": "    return a / b",
                            "new_string": "    if b == 0:\n        return None\n    return a / b",
                        },
                    )
                ]
            )
            if index == 0
            else ScriptedTurn(tool_calls=[("run_tests", {})])
            if index in (1, 2)
            # Iterations are spent; this is the granted final turn.
            else ScriptedTurn(
                text="Budget spent, reporting.",
                tool_calls=[
                    ("finish", {"resolved": True, "summary": "Guarded divide() against b == 0."})
                ],
            )
        )
    )

    result = await run_agent_task(
        make_task("calculator_bug", "run-wrapup-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=provider,
    )
    offered = provider.seen_tools

    assert result.state is RunState.RESOLVED, result.stop_reason
    assert "return None" in result.final_patch
    assert result.metrics.iterations == 3, "the work budget is still enforced"

    # The final turn must offer only `finish`, so it cannot be spent on work.
    assert offered[-1] == ["finish"], offered[-1]
    assert len(offered[-2]) > 1


async def test_the_final_turn_is_granted_only_once(settings, tmp_path) -> None:
    """An agent that ignores the wrap-up turn still terminates."""
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 2

    result = await run_agent_task(
        make_task("calculator_bug", "run-wrapup-2"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        # Never calls finish, not even on the final turn.
        provider=ScriptedProvider(
            responder=lambda messages, index: ScriptedTurn(
                text="still looking", tool_calls=[("list_files", {})]
            )
        ),
    )
    assert result.state is RunState.TIMEOUT
    assert "max_iterations" in result.stop_reason


# ------------------------------------------------- polyglot test discovery


def test_discovery_recognises_each_ecosystem(tmp_path) -> None:
    import json as _json

    from patchpilot.agent.orchestrator import _test_command_for

    (tmp_path / "py").mkdir()
    (tmp_path / "py" / "pyproject.toml").write_text("[project]", encoding="utf-8")
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "package.json").write_text(
        _json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
    )
    (tmp_path / "jsbare").mkdir()
    (tmp_path / "jsbare" / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "go").mkdir()
    (tmp_path / "go" / "go.mod").write_text("module x", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    assert _test_command_for(tmp_path / "py") == "python -m pytest -q"
    assert _test_command_for(tmp_path / "js") == "npm test --silent"
    assert _test_command_for(tmp_path / "jsbare") == "npx jest"
    assert _test_command_for(tmp_path / "go") == "go test ./..."
    assert _test_command_for(tmp_path / "empty") is None


async def test_run_tests_accepts_a_subdirectory(registry, tool_ctx) -> None:
    """A monorepo keeps its suite in a subdirectory.

    Commands never go through a shell, so `cd backend && npx jest` is refused
    by the guard. Without a cwd the agent would be structurally unable to run
    the tests at all — which is exactly what happened on a real TypeScript run.
    """
    workspace = tool_ctx.workspace
    workspace.write_text("backend/tests/test_thing.py", "def test_ok():\n    assert True\n")

    result = await registry.call(
        "run_tests",
        tool_ctx,
        {"command": f"{PY} -m pytest -q tests/", "cwd": "backend"},
    )
    assert result.ok, result.content
    assert result.data["green"] is True
    assert result.data["passed"] == 1
    assert "(backend)" in result.data["command"]


async def test_run_tests_cwd_cannot_escape_the_workspace(registry, tool_ctx) -> None:
    result = await registry.call(
        "run_tests", tool_ctx, {"command": f"{PY} --version", "cwd": "../../etc"}
    )
    assert not result.ok
    assert result.error_code == "path_escape"


def test_discovery_ignores_the_npm_placeholder_test_script(tmp_path) -> None:
    """`npm init` writes a test script that always fails.

    Trusting it makes the agent report "Error: no test specified" instead of
    running the suite the repo actually has — which is exactly what a real
    TypeScript run hit, on a repo that had jest in devDependencies all along.
    """
    import json as _json

    from patchpilot.agent.orchestrator import _test_command_for

    placeholder = 'echo "Error: no test specified" && exit 1'

    (tmp_path / "stub_jest").mkdir()
    (tmp_path / "stub_jest" / "package.json").write_text(
        _json.dumps({"scripts": {"test": placeholder}, "devDependencies": {"jest": "^29"}}),
        encoding="utf-8",
    )
    (tmp_path / "stub_vitest").mkdir()
    (tmp_path / "stub_vitest" / "package.json").write_text(
        _json.dumps({"scripts": {"test": placeholder}, "devDependencies": {"vitest": "^1"}}),
        encoding="utf-8",
    )
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "package.json").write_text(
        _json.dumps({"scripts": {"test": "jest --ci"}}), encoding="utf-8"
    )

    assert _test_command_for(tmp_path / "stub_jest") == "npx jest"
    assert _test_command_for(tmp_path / "stub_vitest") == "npx vitest run"
    assert _test_command_for(tmp_path / "real") == "npm test --silent"
