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
from patchpilot.agent.orchestrator import _declared_test_paths, _test_command_for
from patchpilot.domain.enums import EventType
from patchpilot.events.bus import MemorySink
from patchpilot.repair.progress import Pressure, ProgressTracker
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
    assert tracker.should_warn("pressure:nudge") is True
    assert tracker.should_warn("pressure:nudge") is False
    assert tracker.should_warn("pressure:urgent") is True
    assert tracker.should_warn("pressure:urgent") is False
    # Distinct kinds of advice do not suppress one another.
    assert tracker.should_warn("reinspect:sympy/core/mod.py") is True


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
    assert snap["time_to_first_source_edit"] == 4
    assert snap["time_to_first_test"] == 5
    assert snap["time_to_first_green_test"] == 6


def test_never_editing_is_distinguishable_from_editing_late() -> None:
    """`patch_attempts == 0` read the same for both; None vs int does not."""
    explored = _tracker()
    explored.record_tool("read_file", {"path": "a.py"}, 1, succeeded=True)
    assert explored.snapshot()["time_to_first_source_edit"] is None

    late = _tracker()
    late.record_tool("edit_file", {"path": "a.py"}, 19, succeeded=True)
    assert late.snapshot()["time_to_first_source_edit"] == 19


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
    from patchpilot.repair.progress import is_scratch

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
    assert tracker.snapshot()["time_to_first_source_edit"] == 20


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


# --------------------------------------------- the diagnosis-to-repair gates


def _run_parts():
    from patchpilot.agent.session import SessionConfig, run_agent_task
    from patchpilot.providers.scripted import ScriptedProvider, ScriptedTurn
    from tests.test_agent_loop import make_task

    return make_task, run_agent_task, SessionConfig, ScriptedProvider, ScriptedTurn


async def test_a_scratch_only_diff_cannot_be_finished_as_resolved(settings, tmp_path) -> None:
    """CASE E, end to end. One instance submitted `repro_debug.py` as its whole
    patch and was recorded as having produced one."""
    make_task, run_agent_task, SessionConfig, Provider, Turn = _run_parts()
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 5

    script = [
        Turn(
            text="Reproducing.",
            tool_calls=[("write_file", {"path": "repro_debug.py", "content": "print(1)\n"})],
        ),
        Turn(text="Done.", tool_calls=[("finish", {"resolved": True, "summary": "fixed it"})]),
        Turn(
            text="Fine.",
            tool_calls=[("finish", {"resolved": False, "summary": "could not fix it"})],
        ),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-gate-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=Provider(script=script),
    )

    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    # The workspace strips the scratch file before `finish` ever sees the diff,
    # so the earlier `no_patch` gate catches it. Two independent defences, and
    # the outer one fires first -- which is the right order.
    assert "no_patch" in codes
    assert result.metrics.meaningful_patch is False
    assert result.metrics.time_to_first_source_edit is None
    assert result.metrics.scratch_writes == 1
    assert "repro_debug.py" not in result.final_patch
    assert result.metrics.patch_quality == "no_patch"


async def test_giving_up_with_a_diagnosis_is_challenged_once(settings, tmp_path) -> None:
    """All six empty-patch runs named the right file in their summary and never
    edited it. One refusal converts that prose into an edit -- or is accepted."""
    make_task, run_agent_task, SessionConfig, Provider, Turn = _run_parts()
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 12

    fix = "    if b == 0:\n        return None\n    return a / b"
    script = [
        Turn(text="Reading.", tool_calls=[("read_file", {"path": "src/calculator.py"})]),
        Turn(
            text="Reproducing.",
            tool_calls=[("run_command", {"command": 'python -c "import src.calculator"'})],
        ),
        Turn(text="Giving up.", tool_calls=[("finish", {"resolved": False, "summary": "too hard"})]),
        Turn(
            text="Fixing after all.",
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
        Turn(text="Done.", tool_calls=[("finish", {"resolved": False, "summary": "applied a fix"})]),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-gate-2"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=Provider(script=script),
    )

    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    assert codes.count("diagnosis_without_repair") == 1, f"exactly one push, got {codes}"
    assert result.metrics.meaningful_patch is True
    assert result.metrics.time_to_first_source_edit == 4


async def test_a_run_with_no_diagnosis_may_give_up_immediately(settings, tmp_path) -> None:
    """CASE J. An agent that genuinely established nothing must not be trapped."""
    make_task, run_agent_task, SessionConfig, Provider, Turn = _run_parts()
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 6

    script = [
        Turn(
            text="Nothing found.",
            tool_calls=[("finish", {"resolved": False, "summary": "no idea where to look"})],
        ),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-gate-3"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=Provider(script=script),
    )

    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    assert "diagnosis_without_repair" not in codes
    assert result.metrics.iterations == 1, "it stopped when asked"


async def test_the_repair_push_does_not_repeat(settings, tmp_path) -> None:
    """A second give-up is honoured, so the gate cannot become a trap."""
    make_task, run_agent_task, SessionConfig, Provider, Turn = _run_parts()
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 12

    script = [
        Turn(text="Reading.", tool_calls=[("read_file", {"path": "src/calculator.py"})]),
        Turn(
            text="Reproducing.",
            tool_calls=[("run_command", {"command": 'python -c "import src.calculator"'})],
        ),
        Turn(text="Give up.", tool_calls=[("finish", {"resolved": False, "summary": "blocked"})]),
        Turn(
            text="Still giving up.",
            tool_calls=[("finish", {"resolved": False, "summary": "genuinely blocked"})],
        ),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-gate-4"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=Provider(script=script),
    )

    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    assert codes.count("diagnosis_without_repair") == 1
    assert result.state.is_terminal
    assert result.metrics.meaningful_patch is False


async def test_instrumenting_source_is_not_a_repair_either(settings, tmp_path) -> None:
    """The inner gate: a diff that survives the scratch filter but only adds
    prints to implementation code is still not a fix."""
    make_task, run_agent_task, SessionConfig, Provider, Turn = _run_parts()
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 6

    script = [
        Turn(
            text="Adding a probe.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    return a / b",
                        "new_string": "    print('a', a, 'b', b)\n    return a / b",
                    },
                )
            ],
        ),
        Turn(text="Testing.", tool_calls=[("run_tests", {})]),
        Turn(text="Done.", tool_calls=[("finish", {"resolved": True, "summary": "instrumented"})]),
        Turn(text="Fine.", tool_calls=[("finish", {"resolved": False, "summary": "not fixed"})]),
    ]
    result = await run_agent_task(
        make_task("calculator_bug", "run-gate-5"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=Provider(script=script),
    )

    codes = [c.error_code for c in result.tool_calls if c.tool == "finish"]
    assert "no_meaningful_patch" in codes, f"debug-only diff was accepted: {codes}"
    assert result.metrics.patch_quality == "debug_only"
    assert result.metrics.meaningful_patch is False


async def test_the_empty_patch_trajectory_is_interrupted(settings, tmp_path) -> None:
    """Replays the shape of the six empty-patch runs and asserts we now steer.

    The shape, taken from the trajectories: read the implementation, reproduce
    the failure repeatedly, re-read the same file, never edit. Before this work
    the loop said nothing at all until iteration 4 and then stayed silent; the
    run reached its cap having changed nothing.
    """
    make_task, run_agent_task, SessionConfig, Provider, Turn = _run_parts()
    settings.workspace_root = tmp_path / "ws"
    settings.max_iterations = 12

    def responder(messages, index):
        # Alternate reading the same file and re-running the same reproduction,
        # exactly as the failing runs did.
        if index % 2 == 0:
            return Turn(
                text="Reading again.",
                tool_calls=[("read_file", {"path": "src/calculator.py"})],
            )
        return Turn(
            text="Reproducing again.",
            tool_calls=[("run_command", {"command": 'python -c "import src.calculator"'})],
        )

    sink = MemorySink()
    result = await run_agent_task(
        make_task("calculator_bug", "run-shape-1"),
        settings=settings,
        config=SessionConfig(sandbox="local"),
        provider=Provider(responder=responder),
        sinks=[sink],
    )

    kinds = [str(e.payload.get("kind")) for e in sink.of_type(EventType.RECOVERY_TRIGGERED)]
    assert any(k.startswith("pressure:") for k in kinds), f"never pushed toward a repair: {kinds}"
    assert "pressure:urgent" in kinds, "reproduction capture must escalate to urgent"
    assert any(k.startswith("low_information") or k.startswith("reinspect") for k in kinds), (
        f"re-reading one file was never challenged: {kinds}"
    )

    # And the run is honestly recorded as having produced nothing.
    assert result.metrics.time_to_first_source_edit is None
    assert result.metrics.meaningful_patch is False
    assert result.metrics.reproductions_before_edit >= 3



# --- Reading where a project says its tests live ------------------------------
#
# Detecting tox.ini and then running a bare `pytest` is not the same as reading
# it. jmespath declares `commands = python -m pytest tests` and ships a
# hypothesis fuzzing suite under `extra/` that its own runs never touch; a bare
# pytest from the repository root collected the fuzzer and spent seven minutes
# on it before the agent narrowed the command itself.


def _project(tmp_path, **files: str):
    """A repository root containing the given files, plus a tests/ directory."""
    for name, body in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_it.py").write_text("def test_x(): pass\n", encoding="utf-8")
    return tmp_path


def test_tox_commands_scope_the_test_command(tmp_path) -> None:
    root = _project(tmp_path, **{"tox.ini": "[testenv]\ncommands = python -m pytest tests\n"})
    (root / "extra").mkdir()
    assert _declared_test_paths(root) == ["tests"]
    assert _test_command_for(root) == "python -m pytest -q tests"


def test_testpaths_in_setup_cfg_is_honoured(tmp_path) -> None:
    root = _project(tmp_path, **{"setup.cfg": "[tool:pytest]\ntestpaths = tests\n"})
    assert _test_command_for(root) == "python -m pytest -q tests"


def test_testpaths_in_pyproject_is_honoured(tmp_path) -> None:
    root = _project(
        tmp_path,
        **{"pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'},
    )
    assert _test_command_for(root) == "python -m pytest -q tests"


def test_testpaths_may_be_a_plain_string(tmp_path) -> None:
    root = _project(tmp_path, **{"pytest.ini": "[pytest]\ntestpaths = tests\n"})
    assert _declared_test_paths(root) == ["tests"]


def test_a_declared_path_that_does_not_exist_is_ignored(tmp_path) -> None:
    """A stale declaration must not produce a command that collects nothing."""
    root = _project(tmp_path, **{"pytest.ini": "[pytest]\ntestpaths = gone\n"})
    assert _declared_test_paths(root) == []
    assert _test_command_for(root) == "python -m pytest -q"


def test_a_project_that_declares_nothing_keeps_the_broad_command(tmp_path) -> None:
    """Narrowing on a guess would quietly stop running some of the suite."""
    root = _project(tmp_path, **{"pyproject.toml": "[project]\nname = 'x'\n"})
    assert _declared_test_paths(root) == []
    assert _test_command_for(root) == "python -m pytest -q"


def test_a_flag_value_is_not_mistaken_for_a_test_path(tmp_path) -> None:
    """`--cov jmespath` names a real directory without being a test path."""
    root = _project(
        tmp_path,
        **{"tox.ini": "[testenv]\ncommands = python -m pytest --cov mypkg tests\n"},
    )
    (root / "mypkg").mkdir()
    assert _declared_test_paths(root) == ["tests"]


def test_several_declared_paths_are_all_run(tmp_path) -> None:
    root = _project(tmp_path, **{"pytest.ini": "[pytest]\ntestpaths = tests integration\n"})
    (root / "integration").mkdir()
    assert _test_command_for(root) == "python -m pytest -q tests integration"


def test_an_unparseable_config_falls_back_rather_than_raising(tmp_path) -> None:
    root = _project(tmp_path, **{"pyproject.toml": "this is not : valid = toml ["})
    assert _declared_test_paths(root) == []
    assert _test_command_for(root) == "python -m pytest -q"
