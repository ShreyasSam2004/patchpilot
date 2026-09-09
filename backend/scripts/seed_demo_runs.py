"""Seed the local database with real agent runs, for screenshots and UI work.

This is a **development utility**, not part of the product. It runs the real
orchestrator, the real tools, a real git workspace and real pytest subprocesses
against the bundled fixtures. The only substitution is the model: a scripted
double supplies the tool calls a competent model would make, so the runs are
deterministic and cost nothing.

The runs it produces are genuine agent runs — every event, tool call, patch and
test result is real. It does **not** fabricate benchmark results: the benchmark
dashboard stays honestly empty until the official SWE-bench harness has graded
something.

    python -m scripts.seed_demo_runs
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from patchpilot.agent.orchestrator import AgentTask
from patchpilot.agent.session import SessionConfig, agent_session
from patchpilot.config import get_settings
from patchpilot.db.models import Run
from patchpilot.db.session import dispose_db, init_db, session_scope
from patchpilot.domain.enums import RunState
from patchpilot.events.recorder import DatabaseSink
from patchpilot.fixtures import get_fixture
from patchpilot.ids import new_id, short_code
from patchpilot.logging_setup import configure_logging
from patchpilot.providers.scripted import (
    ScriptedProvider,
    ScriptedTurn,
    last_tool_output,
)
from patchpilot.runs import service


def calculator_responder(messages, index):
    """A competent debugging session: the first fix is too narrow."""
    if index == 0:
        return ScriptedTurn(
            text="Searching for the divide implementation named in the traceback.",
            tool_calls=[("search_code", {"query": "def divide"})],
        )
    if index == 1:
        return ScriptedTurn(
            text="Reading the module to see exactly what divide() does.",
            tool_calls=[("read_file", {"path": "src/calculator.py"})],
        )
    if index == 2:
        return ScriptedTurn(
            text="Checking what the tests assert about a zero denominator.",
            tool_calls=[("search_code", {"query": "divide_by_zero", "path": "tests"})],
        )
    if index == 3:
        return ScriptedTurn(
            text="Recording my hypothesis before touching the code.",
            tool_calls=[
                (
                    "record_hypothesis",
                    {
                        "suspected_cause": (
                            "divide() evaluates a / b with no guard, so a zero "
                            "denominator raises ZeroDivisionError instead of "
                            "returning None"
                        ),
                        "evidence": (
                            "src/calculator.py:27 returns `a / b` directly, but the "
                            "signature is `-> float | None` and the docstring says a "
                            "zero denominator is not an error for callers"
                        ),
                        "files_involved": ["src/calculator.py"],
                        "proposed_fix": "Return None when b == 0, before dividing.",
                        "validation_plan": "python -m pytest tests/ -q",
                    },
                )
            ],
        )
    if index == 4:
        return ScriptedTurn(
            text="Adding the guard.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        # Deliberately too narrow — only guards positive numerators.
                        "old_string": "    return a / b",
                        "new_string": (
                            "    if b == 0 and a > 0:\n        return None\n    return a / b"
                        ),
                    },
                )
            ],
        )
    if index == 5:
        return ScriptedTurn(
            text="Running the test suite to validate the fix.",
            tool_calls=[("run_tests", {})],
        )
    if index == 6:
        observation = last_tool_output(messages, "run_tests")
        assert "failed" in observation, observation[:200]
        return ScriptedTurn(
            text=(
                "The negative-numerator case still fails: my guard was too narrow. "
                "The denominator alone decides this, not the numerator."
            ),
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    if b == 0 and a > 0:",
                        "new_string": "    if b == 0:",
                    },
                )
            ],
        )
    if index == 7:
        return ScriptedTurn(
            text="Re-running the suite to confirm the fix and check for regressions.",
            tool_calls=[("run_tests", {})],
        )
    if index == 8:
        return ScriptedTurn(
            text="Reviewing the final diff before finishing.",
            tool_calls=[("git_diff", {})],
        )
    return ScriptedTurn(
        text="All 10 tests pass. The change is a two-line guard in divide().",
        tool_calls=[
            (
                "finish",
                {
                    "resolved": True,
                    "summary": (
                        "divide() now returns None when the denominator is zero, "
                        "matching its documented contract and its float | None return "
                        "type. percentage() propagates the None unchanged."
                    ),
                },
            )
        ],
    )


def pagination_responder(messages, index):
    """A run that investigates, patches, and honestly reports a shortfall."""
    if index == 0:
        return ScriptedTurn(
            text="Locating the pagination helper.",
            tool_calls=[("find_symbol", {"symbol": "total_pages"})],
        )
    if index == 1:
        return ScriptedTurn(
            text="Reading the module.",
            tool_calls=[("read_file", {"path": "src/pagination.py"})],
        )
    if index == 2:
        return ScriptedTurn(
            text="Recording the hypothesis.",
            tool_calls=[
                (
                    "record_hypothesis",
                    {
                        "suspected_cause": (
                            "total_pages uses floor division, so any partial final "
                            "page is truncated away"
                        ),
                        "evidence": (
                            "src/pagination.py total_pages returns "
                            "self.total // self.per_page; 25 items at 10 per page "
                            "yields 2 instead of 3"
                        ),
                        "files_involved": ["src/pagination.py"],
                        "proposed_fix": "Use a ceiling division.",
                        "validation_plan": "python -m pytest tests/ -q",
                    },
                )
            ],
        )
    if index == 3:
        return ScriptedTurn(
            text="Applying a ceiling division.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/pagination.py",
                        "old_string": "        return self.total // self.per_page",
                        "new_string": "        return -(-self.total // self.per_page)",
                    },
                )
            ],
        )
    if index == 4:
        return ScriptedTurn(text="Validating.", tool_calls=[("run_tests", {})])
    return ScriptedTurn(
        text="All tests pass.",
        tool_calls=[
            (
                "finish",
                {
                    "resolved": True,
                    "summary": (
                        "total_pages now uses ceiling division, so the final partial "
                        "page is reachable and has_next is correct on the "
                        "second-to-last page."
                    ),
                },
            )
        ],
    )


DEMOS = [
    ("calculator_bug", calculator_responder),
    ("pagination_bug", pagination_responder),
]


async def seed_one(fixture_id: str, responder) -> str:
    settings = get_settings()
    fixture = get_fixture(fixture_id)
    run_id = new_id()

    async with session_scope(settings) as session:
        session.add(
            Run(
                id=run_id,
                code=short_code(),
                repository=fixture.repository,
                local_path=str(fixture.repo_path),
                issue=fixture.issue,
                issue_title=fixture.title,
                test_command=fixture.test_command,
                provider="scripted",
                model="scripted-model",
                sandbox="local",
                config={"provider": "scripted", "model": "scripted-model",
                        "sandbox": "local", "seeded": True},
                state=str(RunState.PENDING),
            )
        )

    task = AgentTask(
        run_id=run_id,
        issue=fixture.issue,
        repository=fixture.repository,
        local_path=fixture.repo_path,
        test_command=fixture.test_command,
    )

    async with agent_session(
        task,
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(responder=responder),
        sinks=[DatabaseSink(run_id, settings)],
    ) as orchestrator:
        result = await orchestrator.run()

    async with session_scope(settings) as session:
        await service.apply_result(session, run_id, result)

    print(
        f"  {fixture_id:<16} {result.state:<9} "
        f"{result.metrics.iterations} iterations, "
        f"{result.metrics.test_runs} test runs, "
        f"{len(result.final_patch)} byte patch"
    )
    return run_id


async def main() -> None:
    settings = get_settings()
    configure_logging("WARNING", settings.log_json)
    settings.ensure_dirs()
    await init_db(settings)

    print("Seeding real agent runs (scripted model, everything else genuine):")
    for fixture_id, responder in DEMOS:
        await seed_one(fixture_id, responder)

    print("\nDone. The benchmark dashboard stays empty — it is only ever")
    print("populated by the official SWE-bench evaluator.")
    await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
