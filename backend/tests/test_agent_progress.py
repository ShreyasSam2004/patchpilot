"""Tests for the changes made after the first SWE-bench Lite batch.

That batch produced six runs that hit the iteration cap without ever editing a
file, and 145 of 375 tool calls went on rediscovering the Python interpreter.
Each test here pins one of the mechanisms added in response, and names the
observation that motivated it -- so a future change that quietly reintroduces
the behaviour fails loudly rather than costing another benchmark run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.agent.environment import (
    RuntimeEnvironment,
    apply_to_sandbox,
    probe,
    top_level_package,
)
from patchpilot.agent.progress import Pressure, ProgressTracker
from patchpilot.sandbox.guard import check_command, sanitize_command
from patchpilot.sandbox.local import LocalSandbox

# --------------------------------------------------------------- progress


def _tracker() -> ProgressTracker:
    return ProgressTracker()


def test_pressure_escalates_only_while_nothing_has_been_edited() -> None:
    """The signal is 'no patch yet', not 'no patch recently'."""
    tracker = _tracker()
    assert tracker.pressure(1, 22) is Pressure.NONE
    assert tracker.pressure(10, 22) is Pressure.NUDGE  # 45%
    assert tracker.pressure(18, 22) is Pressure.URGENT  # 70%

    tracker.record_tool("edit_file", {"path": "a.py"}, 4, succeeded=True)
    assert tracker.pressure(21, 22) is Pressure.NONE, "an edit exists; stop nagging"


def test_a_failed_edit_does_not_count_as_progress() -> None:
    tracker = _tracker()
    tracker.record_tool("apply_patch", {"diff": "..."}, 3, succeeded=False)
    assert not tracker.has_edit
    assert tracker.pressure(20, 22) is Pressure.URGENT


def test_each_escalation_is_announced_once() -> None:
    """Advice has to arrive without turning into per-iteration noise."""
    tracker = _tracker()
    assert tracker.should_warn(Pressure.NUDGE) is True
    assert tracker.should_warn(Pressure.NUDGE) is False
    assert tracker.should_warn(Pressure.URGENT) is True
    assert tracker.should_warn(Pressure.URGENT) is False
    assert tracker.should_warn(Pressure.NONE) is False


def test_milestones_record_the_iteration_they_happened_on() -> None:
    tracker = _tracker()
    tracker.record_tool("search_code", {"query": "x"}, 1, succeeded=True)
    tracker.record_tool("read_file", {"path": "sympy/core/mod.py"}, 2, succeeded=True)
    tracker.record_tool("record_hypothesis", {}, 3, succeeded=True)
    tracker.record_tool("edit_file", {"path": "sympy/core/mod.py"}, 4, succeeded=True)
    tracker.record_test(5, green=False)
    tracker.record_test(6, green=True)

    snap = tracker.snapshot()
    assert snap["time_to_first_file_read"] == 2
    assert snap["time_to_first_hypothesis"] == 3
    assert snap["time_to_first_edit"] == 4
    assert snap["time_to_first_test"] == 5
    assert snap["time_to_first_green_test"] == 6


def test_never_editing_is_distinguishable_from_editing_late() -> None:
    """`patch_attempts == 0` read the same for both; None vs int does not."""
    explored = _tracker()
    explored.record_tool("read_file", {"path": "a.py"}, 1, succeeded=True)
    assert explored.snapshot()["time_to_first_edit"] is None

    late = _tracker()
    late.record_tool("edit_file", {"path": "a.py"}, 19, succeeded=True)
    assert late.snapshot()["time_to_first_edit"] == 19


def test_the_last_file_read_is_offered_as_the_edit_target() -> None:
    tracker = _tracker()
    assert tracker.best_candidate() is None
    tracker.record_tool("read_file", {"path": "one.py"}, 1, succeeded=True)
    tracker.record_tool("read_file", {"path": "two.py"}, 2, succeeded=True)
    assert tracker.best_candidate() == "two.py"


# ------------------------------------------------------------ command guard


@pytest.mark.parametrize(
    ("argv", "expected", "cwd"),
    [
        # Every one of these forms actually appeared in the batch, and every one
        # was refused, costing an iteration to learn and another to redo.
        (["ls", "/opt", "2>/dev/null"], ["ls", "/opt"], None),
        (["python", "-c", "import x", "2>&1"], ["python", "-c", "import x"], None),
        (["git", "log", "|", "head", "-20"], ["git", "log"], None),
        (["pip", "show", "mpmath", "|", "head"], ["pip", "show", "mpmath"], None),
        (["cd", "/tmp", "&&", "python", "repro.py"], ["python", "repro.py"], "/tmp"),
    ],
)
def test_inert_shell_decoration_is_stripped(
    argv: list[str], expected: list[str], cwd: str | None
) -> None:
    cleaned = sanitize_command(argv)
    assert cleaned.argv == expected
    assert cleaned.cwd == cwd
    assert cleaned.notes, "the agent must be told what was rewritten"
    assert check_command(cleaned.argv), "the cleaned command must now be allowed"


@pytest.mark.parametrize(
    "argv",
    [
        # A real pipeline and a real command chain still need a shell, so they
        # stay refused rather than being silently half-executed.
        ["pytest", "-q", "|", "grep", "FAIL"],
        ["which", "python", ";", "pip", "show", "mpmath"],
        ["ls", "|", "xargs", "rm"],
    ],
)
def test_real_pipelines_are_left_for_the_guard_to_refuse(argv: list[str]) -> None:
    cleaned = sanitize_command(argv)
    assert cleaned.argv == argv, "sanitising must not half-execute a real pipeline"
    assert not check_command(cleaned.argv)


def test_ordinary_commands_are_untouched() -> None:
    for argv in (["python", "-m", "pytest", "-q"], ["git", "diff"], ["python", "bin/test", "x"]):
        cleaned = sanitize_command(argv)
        assert cleaned.argv == argv
        assert cleaned.cwd is None
        assert cleaned.notes == []


def test_a_semicolon_inside_python_source_is_still_inert() -> None:
    """Regression: `python -c "import numpy; print(x)"` is not a shell chain."""
    argv = ["python", "-c", "import numpy; print(numpy.__version__)"]
    assert sanitize_command(argv).argv == argv
    assert check_command(argv)


# ------------------------------------------------------------- environment


def test_top_level_package_prefers_the_one_named_after_the_repo(tmp_path: Path) -> None:
    root = tmp_path / "sympy"
    (root / "sympy").mkdir(parents=True)
    (root / "sympy" / "__init__.py").write_text("")
    (root / "doc").mkdir()
    (root / "doc" / "__init__.py").write_text("")
    assert top_level_package(root) == "sympy"


def test_top_level_package_ignores_tests_and_docs(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    for name in ("tests", "docs", "scripts"):
        (root / name).mkdir(parents=True)
        (root / name / "__init__.py").write_text("")
    (root / "mylib").mkdir()
    (root / "mylib" / "__init__.py").write_text("")
    assert top_level_package(root) == "mylib"


def test_top_level_package_is_none_when_there_is_no_package(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    assert top_level_package(tmp_path) is None


async def test_probe_finds_the_running_interpreter(tmp_path: Path) -> None:
    """The probe must work against a real sandbox, not only a stub."""
    sandbox = LocalSandbox(tmp_path)
    env = await probe(sandbox, tmp_path)
    assert env.probed
    assert env.usable, "the test runner's own interpreter should be discoverable"
    assert env.has_pytest, "pytest is installed in this environment"


async def test_the_probe_puts_the_interpreter_first_on_path(tmp_path: Path) -> None:
    sandbox = LocalSandbox(tmp_path)
    env = await probe(sandbox, tmp_path)
    await apply_to_sandbox(sandbox, env)
    assert sandbox.env_overlay["PATH"].startswith(env.bin_dir or "")


async def test_an_unusable_environment_changes_nothing(tmp_path: Path) -> None:
    """A failed probe must degrade to the previous behaviour, never break a run."""
    sandbox = LocalSandbox(tmp_path)
    await apply_to_sandbox(sandbox, RuntimeEnvironment(probed=True))
    assert sandbox.env_overlay == {}


def test_the_briefing_tells_the_agent_not_to_go_hunting() -> None:
    """This wording is the fix for 39% of the batch's tool calls."""
    described = RuntimeEnvironment(
        python="/opt/miniconda3/envs/testbed/bin/python",
        bin_dir="/opt/miniconda3/envs/testbed/bin",
        has_pytest=True,
        package="sympy",
        imports_project=True,
        probed=True,
    ).describe()
    assert "/opt/miniconda3/envs/testbed/bin/python" in described
    assert "PATH" in described
    for discouraged in ("virtualenv", "conda", "Do not spend turns"):
        assert discouraged in described


def test_the_briefing_is_honest_when_pytest_is_missing() -> None:
    """Handing over `python -m pytest` when pytest is absent cost whole runs."""
    described = RuntimeEnvironment(
        python="/usr/bin/python3", bin_dir="/usr/bin", has_pytest=False, probed=True
    ).describe()
    assert "not installed" in described.lower()
    assert "no network" in described


# ------------------------------------------------- end to end through the loop


async def test_a_real_run_records_when_it_first_edited(settings, tmp_path) -> None:
    """The loop must actually populate the new diagnostics, not just define them."""
    from patchpilot.agent.session import SessionConfig, run_agent_task
    from patchpilot.providers.scripted import ScriptedProvider, ScriptedTurn
    from tests.test_agent_loop import make_task

    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 6

    fix = "    if b == 0:\n        return None\n    return a / b"
    script = [
        ScriptedTurn(text="Reading.", tool_calls=[("read_file", {"path": "src/calculator.py"})]),
        ScriptedTurn(
            text="Patching.",
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
        ScriptedTurn(text="Done.", tool_calls=[("finish", {"resolved": False, "summary": "explored but did not fix it"})]),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-progress-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(script=script),
    )

    metrics = result.metrics.to_dict()
    assert metrics["time_to_first_file_read"] == 1
    assert metrics["time_to_first_edit"] == 2
    assert metrics["verified_by_tests"] is False, "no test was ever run"


async def test_a_green_run_that_predates_the_last_edit_cannot_close_the_run(
    settings, tmp_path
) -> None:
    """The calibration hole the batch exposed, reduced to its mechanism.

    `finish` already required a green test run. It did not require that green
    to be *about the code being submitted*, so edit -> test -> edit -> finish
    passed the guard on a result that predated the final change. The agent
    called 5 instances RESOLVED and the harness confirmed 2.
    """
    from patchpilot.agent.session import SessionConfig, run_agent_task
    from patchpilot.providers.scripted import ScriptedProvider, ScriptedTurn
    from tests.test_agent_loop import make_task

    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 6

    def edit(new: str) -> ScriptedTurn:
        return ScriptedTurn(
            text="Editing.",
            tool_calls=[
                (
                    "edit_file",
                    {"path": "src/calculator.py", "old_string": "    return a / b",
                     "new_string": new},
                )
            ],
        )

    script = [
        edit("    if b == 0:\n        return None\n    return a / b"),
        ScriptedTurn(text="Testing.", tool_calls=[("run_tests", {})]),
        # A further edit invalidates the green above.
        ScriptedTurn(
            text="One more tweak.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    if b == 0:" + chr(10) + "        return None",
                        "new_string": "    if b == 0:" + chr(10) + "        return None  # guard",
                    },
                )
            ],
        ),
        ScriptedTurn(
            text="Declaring victory.",
            tool_calls=[("finish", {"resolved": True, "summary": "fixed the divide guard"})],
        ),
        ScriptedTurn(
            text="Re-running as instructed.", tool_calls=[("run_tests", {})]
        ),
        ScriptedTurn(
            text="Now finishing.",
            tool_calls=[("finish", {"resolved": True, "summary": "fixed the divide guard"})],
        ),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-progress-2"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=ScriptedProvider(script=script),
    )

    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    assert "tests_stale" in codes, f"the stale green was accepted: {codes}"
    # After re-running, the claim is backed by evidence again.
    assert result.agent_claimed_resolved is True
    assert result.metrics.verified_by_tests is True
    assert result.metrics.time_to_first_edit == 1
    assert result.metrics.time_to_first_green_test == 2


# ---------------------------------------------------- adaptive iteration budget


def _tracker_with_edit() -> ProgressTracker:
    tracker = ProgressTracker()
    tracker.record_tool("edit_file", {"path": "a.py"}, 2, succeeded=True)
    return tracker


def test_the_budget_extension_is_granted_once_and_is_bounded() -> None:
    from patchpilot.agent.budget import Budget, BudgetTracker

    tracker = BudgetTracker(Budget(max_iterations=22))
    assert tracker.grant_iteration_extension(6) is True
    assert tracker.budget.max_iterations == 28
    assert tracker.grant_iteration_extension(6) is False, "one extension only"
    assert tracker.budget.max_iterations == 28
    assert tracker.grant_iteration_extension(0) is False


def test_the_extension_does_not_touch_the_cost_or_time_ceilings() -> None:
    """Extra turns must not become extra spend beyond what was authorised."""
    from patchpilot.agent.budget import Budget, BudgetTracker

    tracker = BudgetTracker(Budget(max_iterations=10, max_cost_usd=2.0, max_runtime_s=900))
    tracker.grant_iteration_extension(3)
    assert tracker.budget.max_cost_usd == 2.0
    assert tracker.budget.max_runtime_s == 900
    tracker.record_cost(2.5)
    assert tracker.exceeded() == "max_cost_usd", "cost still stops the run"


# ------------------------------------------------- test-runner discovery


def test_pytest_is_not_suggested_when_the_interpreter_lacks_it(tmp_path: Path) -> None:
    """Verified against a real SWE-bench image: sympy's testbed env has no pytest.

    The tree has a `conftest.py`, so the old heuristic returned
    `python -m pytest -q` -- which exits 1 with no output at all. Every task in
    the first batch was handed exactly that.
    """
    from patchpilot.agent.orchestrator import _test_command_for

    (tmp_path / "conftest.py").write_text("")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "test").write_text("#!/usr/bin/env python\n")

    assert _test_command_for(tmp_path, has_pytest=True) == "python -m pytest -q"
    assert _test_command_for(tmp_path, has_pytest=False) == "python bin/test"


def test_django_style_runners_are_recognised(tmp_path: Path) -> None:
    from patchpilot.agent.orchestrator import _test_command_for

    (tmp_path / "conftest.py").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "runtests.py").write_text("")
    assert _test_command_for(tmp_path, has_pytest=False) == "python tests/runtests.py"


def test_unittest_is_the_fallback_without_pytest(tmp_path: Path) -> None:
    from patchpilot.agent.orchestrator import _test_command_for

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    assert _test_command_for(tmp_path, has_pytest=False) == "python -m unittest discover"
    assert _test_command_for(tmp_path, has_pytest=True) == "python -m pytest -q"


# ------------------------------------------------------- scratch files


@pytest.mark.parametrize(
    ("path", "scratch"),
    [
        (".patchpilot/repro.py", True),
        ("./.patchpilot/notes.py", True),
        ("repro_debug.py", True),      # this one *was* an entire submitted patch
        ("repro_test2.py", True),      # this one shipped alongside a real fix
        ("check.py", True),
        ("sympy/core/mod.py", False),
        ("tests/test_numbers.py", False),
        ("sympy/repro_helper.py", False),  # inside a package: could be a real fix
    ],
)
def test_scratch_paths_are_recognised(path: str, scratch: bool) -> None:
    from patchpilot.agent.progress import is_scratch

    assert is_scratch(path) is scratch


def test_writing_a_repro_script_is_not_progress_toward_a_patch() -> None:
    """Observed: a run wrote `repro_debug.py` at iteration 19, which silenced the
    anti-paralysis nudge and became the whole patch. It had changed no source."""
    tracker = ProgressTracker()
    tracker.record_tool("write_file", {"path": "repro_debug.py"}, 19, succeeded=True)
    assert not tracker.has_edit
    assert tracker.pressure(20, 22) is Pressure.URGENT, "the nudge must keep firing"

    tracker.record_tool("edit_file", {"path": "sympy/core/numbers.py"}, 20, succeeded=True)
    assert tracker.has_edit
    assert tracker.snapshot()["time_to_first_edit"] == 20


async def test_a_repro_script_in_the_repo_root_is_kept_out_of_the_patch(
    workspace,
) -> None:
    """The safety net for when the agent ignores `.patchpilot/` anyway."""
    (workspace.root / "repro_debug.py").write_text("print('debug')\n", encoding="utf-8")
    (workspace.root / "src").mkdir(exist_ok=True)
    real = workspace.root / "src" / "calculator.py"
    real.write_text((real.read_text(encoding="utf-8") if real.exists() else "") + "# fix\n",
                    encoding="utf-8")

    diff = await workspace.diff()
    assert "repro_debug.py" not in diff, "a debug script must never reach the patch"
    assert "src/calculator.py" in diff, "the real change must survive"


async def test_a_new_module_inside_a_package_still_reaches_the_patch(workspace) -> None:
    """The exclusion is narrow: adding a real source file can be a genuine fix."""
    pkg = workspace.root / "src"
    pkg.mkdir(exist_ok=True)
    (pkg / "repro_helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    diff = await workspace.diff()
    assert "src/repro_helper.py" in diff
