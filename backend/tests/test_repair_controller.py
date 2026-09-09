"""Controller behaviour: does the loop push a diagnosis into a source repair?

Each test names the trajectory it comes from. The second benchmark pass showed
that all six empty-patch runs had localised correctly -- five named the exact
function in their final summary -- and that what separated them from the runs
that shipped a patch was purely the order of operations:

    verdict        reproductions BEFORE first edit    reproductions AFTER
    EMPTY_PATCH       10, 7, 5, 2, 1, 0                     none
    patched            none                               2 to 7

These are the cases that must stay true, including the negative one: the point
is anti-paralysis *without* reckless editing, so a run with a real reason to
keep looking must still be allowed to look.
"""

from __future__ import annotations

import pytest

from patchpilot.repair.patchquality import PatchQuality, assess_patch
from patchpilot.repair.progress import (
    Pressure,
    ProgressTracker,
    WriteKind,
    classify_write,
    looks_like_reproduction,
)

BUDGET = 22


def tracker() -> ProgressTracker:
    return ProgressTracker()


def read(t: ProgressTracker, path: str, iteration: int, tool: str = "read_file") -> None:
    t.record_tool(tool, {"path": path}, iteration, succeeded=True)


def reproduce(t: ProgressTracker, iteration: int, snippet: str = "from sympy import *") -> None:
    t.record_tool("run_command", {"command": f'python -c "{snippet}"'}, iteration, succeeded=True)


def edit(t: ProgressTracker, path: str, iteration: int) -> None:
    t.record_tool("edit_file", {"path": path}, iteration, succeeded=True)


# --- CASE A: reads the same file forever, never edits -------------------------


def test_case_a_repeated_reading_raises_pressure_and_is_named() -> None:
    t = tracker()
    for i in range(1, 7):
        read(t, "sympy/core/numbers.py", i)

    assert t.over_inspected() == "sympy/core/numbers.py"
    assert t.localised
    assert not t.has_source_edit
    # 6/22 is only 27% of budget, so the clock alone would say nothing yet.
    assert t.pressure(6, BUDGET) is Pressure.NONE, "no diagnosis yet, so no push"

    reproduce(t, 7)
    assert t.has_diagnosis
    assert t.pressure(7, BUDGET) is Pressure.NUDGE, "evidence, not the clock, triggers this"


# --- CASE B: writes a debug script --------------------------------------------


def test_case_b_a_debug_script_is_not_a_source_edit() -> None:
    """`repro_debug.py` was one instance's entire submitted patch."""
    t = tracker()
    t.record_tool("write_file", {"path": "repro_debug.py"}, 19, succeeded=True)

    assert not t.has_source_edit
    assert t.scratch_writes == 1
    assert t.first_scratch_write == 19
    assert t.first_source_edit is None
    assert t.snapshot()["time_to_first_source_edit"] is None
    assert t.pressure(19, BUDGET) is Pressure.URGENT, "the nudge must keep firing"


def test_case_b_the_scratch_directory_is_also_not_a_source_edit() -> None:
    t = tracker()
    t.record_tool("write_file", {"path": ".patchpilot/repro.py"}, 2, succeeded=True)
    assert not t.has_source_edit
    assert t.first_any_write == 2 and t.first_source_edit is None


# --- CASE C: root cause + target located --------------------------------------


def test_case_c_localised_and_reproduced_prefers_a_repair() -> None:
    t = tracker()
    read(t, "sympy/simplify/trigsimp.py", 2)
    reproduce(t, 3)

    assert t.has_diagnosis
    assert t.best_candidate() == "sympy/simplify/trigsimp.py"
    assert t.pressure(4, BUDGET) is Pressure.NUDGE, "early, but the evidence is there"


def test_case_c_an_explicit_hypothesis_also_counts() -> None:
    """`record_hypothesis` was used in only 7 of 20 runs, so it cannot be
    required -- but when it is used it must count."""
    t = tracker()
    read(t, "sympy/core/mod.py", 2)
    t.record_tool("record_hypothesis", {}, 3, succeeded=True)
    assert t.has_diagnosis
    assert t.first_hypothesis == 3


# --- CASE D: patch then failure -> revise, not restart ------------------------


def test_case_d_a_failing_patch_does_not_reopen_the_search() -> None:
    t = tracker()
    read(t, "sympy/core/mod.py", 2)
    edit(t, "sympy/core/mod.py", 3)
    t.record_test(4, green=False)

    # Evidence is retained, and no implementation pressure is applied: the run
    # has a patch to revise, which is a different situation from having none.
    assert t.pressure(20, BUDGET) is Pressure.NONE
    assert t.source_files_touched == {"sympy/core/mod.py"}
    assert t.best_candidate() == "sympy/core/mod.py"

    edit(t, "sympy/core/mod.py", 5)
    assert t.source_edits == 2, "a revision is another source edit, not a restart"


# --- CASE E: only scratch in the diff -----------------------------------------


def test_case_e_a_scratch_only_diff_is_not_a_patch() -> None:
    diff = (
        "diff --git a/repro_debug.py b/repro_debug.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/repro_debug.py\n"
        "@@ -0,0 +1,2 @@\n+import sympy\n+print(hash(a) == hash(b))\n"
    )
    assessment = assess_patch(diff)
    assert assessment.quality is PatchQuality.SCRATCH_ONLY
    assert not assessment.is_repair
    assert "repro_debug.py" in assessment.describe()


def test_case_e_instrumentation_of_real_source_is_also_not_a_repair() -> None:
    diff = (
        "diff --git a/sympy/core/mod.py b/sympy/core/mod.py\n"
        "--- a/sympy/core/mod.py\n+++ b/sympy/core/mod.py\n"
        "@@ -1,3 +1,4 @@\n+    print('p', p, 'q', q)\n"
    )
    assert assess_patch(diff).quality is PatchQuality.DEBUG_ONLY


def test_case_e_a_real_edit_survives_alongside_debugging() -> None:
    """Conservative on purpose: suppressing a genuine fix is the worse error."""
    diff = (
        "diff --git a/sympy/core/mod.py b/sympy/core/mod.py\n"
        "--- a/sympy/core/mod.py\n+++ b/sympy/core/mod.py\n"
        "@@ -1,3 +1,5 @@\n+    print('debug')\n+    return abs(p) % q\n"
    )
    assert assess_patch(diff).is_repair


# --- CASE F/H: verification evidence ------------------------------------------


def test_case_f_a_green_test_before_the_edit_is_not_verification() -> None:
    """Covered end to end in test_agent_progress; this pins the tracker half."""
    t = tracker()
    edit(t, "sympy/core/mod.py", 3)
    t.record_test(4, green=True)
    assert t.first_green_test == 4
    edit(t, "sympy/core/mod.py", 5)
    # A later edit means the green no longer describes the tree being submitted.
    assert t.first_source_edit == 3 and t.source_edits == 2


def test_case_h_a_behaviour_check_after_the_edit_is_recorded() -> None:
    """Every resolved run reproduced after editing; no empty-patch run did."""
    t = tracker()
    reproduce(t, 1)
    edit(t, "sympy/functions/elementary/hyperbolic.py", 2)
    reproduce(t, 3)

    assert t.reproductions_before_edit == 1
    assert t.first_behaviour_check == 3
    assert t.behaviour_checked


# --- CASE I: late budget, stable hypothesis, no source edit -------------------


def test_case_i_reproduction_capture_is_caught_before_the_budget_runs_out() -> None:
    """The exact shape of the six empty-patch runs, which reproduced 1-10 times."""
    t = tracker()
    read(t, "sympy/simplify/trigsimp.py", 2)
    for i in range(3, 6):
        reproduce(t, i)

    assert t.reproductions_before_edit == 3
    # Iteration 6 of 22 is 27% -- far below the clock thresholds. The evidence
    # is what fires this, which is the whole point: waiting for 70% is how those
    # runs got to reproduce ten times.
    assert t.pressure(6, BUDGET) is Pressure.URGENT


def test_case_i_late_budget_alone_still_escalates() -> None:
    t = tracker()
    assert t.pressure(16, BUDGET) is Pressure.URGENT, "70% with nothing changed"


# --- CASE J: legitimate further investigation is still allowed ----------------


def test_case_j_a_run_that_is_still_localising_is_left_alone() -> None:
    """Anti-paralysis must not become anti-thinking."""
    t = tracker()
    t.record_tool("search_code", {"query": "exptrigsimp"}, 1, succeeded=True)
    read(t, "sympy/simplify/trigsimp.py", 2)

    assert not t.has_diagnosis, "reading alone is not yet a diagnosis"
    assert t.pressure(2, BUDGET) is Pressure.NONE
    assert t.pressure(3, BUDGET) is Pressure.NONE
    assert t.over_inspected() is None


def test_case_j_reading_several_different_files_is_not_over_inspection() -> None:
    t = tracker()
    for i, path in enumerate(["a/one.py", "a/two.py", "a/three.py", "a/four.py"], start=1):
        read(t, path, i)
    assert t.over_inspected() is None, "breadth is localisation, not repetition"


def test_case_j_pressure_stops_once_a_source_edit_exists() -> None:
    t = tracker()
    read(t, "sympy/core/mod.py", 2)
    reproduce(t, 3)
    edit(t, "sympy/core/mod.py", 4)
    assert t.pressure(21, BUDGET) is Pressure.NONE, "it has a patch; let it work"


# --- supporting classification ------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("sympy/core/numbers.py", WriteKind.SOURCE),
        ("src/pkg/module.py", WriteKind.SOURCE),
        ("sympy/core/tests/test_numbers.py", WriteKind.TEST),
        ("tests/test_x.py", WriteKind.TEST),
        ("pkg/foo_test.py", WriteKind.TEST),
        ("repro_debug.py", WriteKind.SCRATCH),
        (".patchpilot/repro.py", WriteKind.SCRATCH),
    ],
)
def test_writes_are_classified(path: str, kind: WriteKind) -> None:
    assert classify_write(path) is kind


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('python -c "from sympy import *; print(trigsimp(f))"', True),
        ("python .patchpilot/repro.py", True),
        ("python -m pytest sympy/core/tests -q", False),
        ("python bin/test sympy/core", False),
        ("git diff", False),
        ('python -c "print(1)"', False),  # no import: not exercising the project
    ],
)
def test_reproductions_are_recognised(command: str, expected: bool) -> None:
    assert looks_like_reproduction(command) is expected


def test_a_test_edit_is_neither_scratch_nor_the_repair() -> None:
    t = tracker()
    t.record_tool("edit_file", {"path": "sympy/core/tests/test_mod.py"}, 4, succeeded=True)
    assert t.test_edits == 1
    assert not t.has_source_edit, "editing tests is not fixing the bug"
    assert t.first_test_edit == 4


# --- environment fallbacks (STEP 16: do not regress the previous pass) --------


def test_pytest_is_only_ruled_out_by_a_successful_probe() -> None:
    """A probe that found no interpreter knows nothing about pytest.

    Treating "probe failed" as "no pytest" would push every ordinary Python
    project off pytest and onto a guessed runner, which is a worse default than
    the one the previous pass replaced.
    """
    from patchpilot.agent.environment import RuntimeEnvironment

    failed = RuntimeEnvironment(probed=True)
    assert not failed.usable
    assert (failed.has_pytest or not failed.usable) is True, "fall back to pytest"

    without = RuntimeEnvironment(python="/usr/bin/python3", probed=True, has_pytest=False)
    assert without.usable
    assert (without.has_pytest or not without.usable) is False, "trust a real answer"

    with_pytest = RuntimeEnvironment(python="/usr/bin/python3", probed=True, has_pytest=True)
    assert (with_pytest.has_pytest or not with_pytest.usable) is True
