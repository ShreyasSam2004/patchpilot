"""The verification signal, traced from raw runner output to the agent's verdict.

Parsing SymPy correctly is only half the fix. The forensic finding was that
`verified_by_tests` had become a synonym for "the command exited 0", because a
run whose summary nobody understood still reached the orchestrator as
``green=True``. These tests follow one deterministic run all the way through:

    raw output -> parse_test_output -> run_tests data -> TestRunRecord
              -> ProgressTracker / verified_by_tests -> finish gate

No model is involved: a scripted provider drives the loop and the sandbox runs
a real command that prints canned runner output.
"""

from __future__ import annotations

import sys

import pytest

from patchpilot.agent.result import TestRunRecord as RunRecord  # avoid pytest collection
from patchpilot.agent.session import SessionConfig, run_agent_task
from patchpilot.providers.scripted import ScriptedProvider, ScriptedTurn
from patchpilot.tools.native.testparse import parse_test_output
from tests.test_agent_loop import make_task

PY = sys.executable

SYMPY_FAIL = (
    "________________________________________________________________________________\n"
    "___________ sympy/printing/tests/test_ccode.py:test_ccode_Relational ___________\n"
    "    assert ccode(Eq(x, y)) == 'x == y'\n"
    "AssertionError\n"
    "============= tests finished: 30 passed, 2 failed, in 2.39 seconds =============\n"
)
SYMPY_PASS = "================== tests finished: 27 passed, in 1.86 seconds ==================\n"


def _emit(text: str, exit_code: int) -> list[str]:
    """A real command that prints canned runner output inside the sandbox.

    Passed as an argv list so no shell quoting or tokenisation stands between
    the fixture and the parser -- the point is to exercise the real
    run_tests -> parse -> record path, not shlex.
    """
    return [PY, "-c", f"import sys; sys.stdout.write({text!r}); sys.exit({exit_code})"]


# --- the record carries the signal --------------------------------------------


def test_the_record_preserves_counts_names_and_recognition() -> None:
    outcome = parse_test_output(SYMPY_FAIL, "", 1)
    record = RunRecord(
        iteration=1,
        command="python bin/test sympy/printing/tests/test_ccode.py",
        passed=outcome.passed,
        failed=outcome.failed,
        errors=outcome.errors,
        skipped=outcome.skipped,
        total=outcome.total,
        green=outcome.green,
        duration_ms=100,
        framework=outcome.framework,
        summary_recognized=outcome.parsed,
        xfail=outcome.xfail,
        failing_tests=outcome.failing_tests,
    )
    as_dict = record.to_dict()
    assert as_dict["framework"] == "sympy"
    assert as_dict["passed"] == 30
    assert as_dict["failed"] == 2
    assert as_dict["green"] is False
    assert as_dict["summary_recognized"] is True
    assert as_dict["failing_tests"] == ["test_ccode_Relational"]


# --- the loop, end to end ------------------------------------------------------


async def _run(settings, tmp_path, run_id: str, output: str, exit_code: int):
    """Edit a source file, then run a command that prints canned runner output."""
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 6
    # These tests are about how runner output is read. The script gives one
    # verdict and concedes if it is refused, which cannot also model the
    # edge-case review that follows a green claim; that has its own tests.
    settings.edge_review = False
    script = [
        ScriptedTurn(
            text="Patching.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    return a / b",
                        "new_string": "    if b == 0:\n        return None\n    return a / b",
                    },
                )
            ],
        ),
        ScriptedTurn(text="Testing.", tool_calls=[("run_tests", {"command": _emit(output, exit_code)})]),
        ScriptedTurn(
            text="Reporting.",
            tool_calls=[("finish", {"resolved": True, "summary": "applied the guard"})],
        ),
        ScriptedTurn(
            text="Conceding.",
            tool_calls=[("finish", {"resolved": False, "summary": "tests are not green"})],
        ),
    ]
    return await run_agent_task(
        make_task("calculator_bug", run_id),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(script=script),
    )


async def test_a_failing_sympy_run_reaches_the_agent_as_a_failure(settings, tmp_path) -> None:
    """The core regression: 30 passed / 2 failed must not become 'tests green'."""
    result = await _run(settings, tmp_path, "run-flow-1", SYMPY_FAIL, 1)

    record = result.test_runs[-1]
    assert record.framework == "sympy"
    assert (record.passed, record.failed) == (30, 2)
    assert record.green is False
    assert record.summary_recognized is True
    assert record.failing_tests == ["test_ccode_Relational"]

    # It must not be treated as verification, and `finish` must refuse success.
    assert result.metrics.verified_by_tests is False
    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    assert "tests_failing" in codes

    # And the agent must be able to see which test failed.
    feedback = next(c.summary for c in result.tool_calls if c.tool == "run_tests")
    assert "30 passed" in feedback and "2 failed" in feedback


async def test_a_passing_sympy_run_counts_as_real_verification(settings, tmp_path) -> None:
    result = await _run(settings, tmp_path, "run-flow-2", SYMPY_PASS, 0)

    record = result.test_runs[-1]
    assert record.framework == "sympy"
    assert record.passed == 27
    assert record.green is True
    assert record.summary_recognized is True
    assert result.metrics.verified_by_tests is True, "a parsed, clean run is evidence"
    assert result.agent_claimed_resolved is True


async def test_an_unparsed_run_that_exits_zero_is_not_verification(settings, tmp_path) -> None:
    """This is exactly what happened for every SymPy instance before the fix.

    The command succeeds and nothing recognisable is printed. `green` may still
    be True -- that fallback is what keeps unsupported runners usable -- but it
    must not be recorded as having verified anything.
    """
    result = await _run(settings, tmp_path, "run-flow-3", "some unrecognised output\n", 0)

    record = result.test_runs[-1]
    assert record.framework == "unknown"
    assert record.summary_recognized is False
    assert (record.passed, record.failed) == (0, 0)
    assert record.green is True, "exit-code fallback is unchanged"
    assert result.metrics.verified_by_tests is False, (
        "an unparsed run must never masquerade as verification"
    )


@pytest.mark.parametrize(
    ("output", "exit_code", "expect_names"),
    [
        (SYMPY_FAIL, 1, ["test_ccode_Relational"]),
        ("===== 1 failed, 3 passed in 0.50s =====\nFAILED tests/test_x.py::test_y\n", 1,
         ["tests/test_x.py::test_y"]),
    ],
)
async def test_failure_driven_repair_receives_named_tests(
    settings, tmp_path, output: str, exit_code: int, expect_names: list[str]
) -> None:
    """Whichever runner is in use, a failure must name what failed."""
    result = await _run(settings, tmp_path, f"run-flow-{hash(output) & 0xffff}", output, exit_code)
    record = result.test_runs[-1]
    assert record.green is False
    assert record.failing_tests == expect_names
    assert record.summary_recognized is True
