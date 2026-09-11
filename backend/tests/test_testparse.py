"""Test-output parsing, especially SymPy's native runner.

Why this file exists. PatchPilot correctly switched old SymPy checkouts onto
`python bin/test`, because their interpreter has no pytest. The parser only
understood pytest, so every one of those runs came back::

    framework=unknown  passed=0  failed=0  errors=0  failing_tests=[]

The agent was running exactly the right tests and learning nothing from them,
and `verified_by_tests` decayed into "the command exited 0" for a whole
benchmark family.

Every SymPy sample below is a real summary line taken from stored harness logs
under `data/swebench/*/logs/run_evaluation/`, not invented from a spec -- the
decorative padding varies with terminal width and the longest form wraps onto a
second line, neither of which a made-up example would have shown.
"""

from __future__ import annotations

import pytest

from patchpilot.tools.native.testparse import (
    TestOutcome as Outcome,  # aliased: pytest would try to collect a 'Test*' class
)
from patchpilot.tools.native.testparse import (
    extract_failure_excerpt,
    parse_test_output,
)

# --- real SymPy summaries, verbatim from stored logs -------------------------

SYMPY_ALL_PASS = "================== tests finished: 27 passed, in 1.86 seconds =================="
SYMPY_TWO_FAILED = (
    "============= tests finished: 30 passed, 2 failed, in 2.39 seconds ============="
)
SYMPY_ONE_FAILED = (
    "============= tests finished: 9 passed, 1 failed, in 0.45 seconds =============="
)
SYMPY_XFAIL_AND_EXC = (
    " tests finished: 79 passed, 1 failed, 1 expected to fail, 1 exceptions, \n"
    "in 4.29 seconds "
)
SYMPY_EXCEPTIONS_ONLY = (
    "= tests finished: 37 passed, 2 expected to fail, 9 exceptions, in 8.44 seconds ="
)
SYMPY_SKIPPED_XFAIL = (
    "== tests finished: 83 passed, 1 skipped, 1 expected to fail, in 12.19 seconds =="
)

SYMPY_FAILURE_BANNERS = """\
________________________________________________________________________________
___________ sympy/printing/tests/test_ccode.py:test_ccode_Relational ___________
  File "/testbed/sympy/printing/tests/test_ccode.py", line 125, in test_ccode_Relational
    assert ccode(Eq(x, y)) == "x == y"
AssertionError
________________________________________________________________________________
______________ sympy/printing/tests/test_ccode.py:test_ccode_sinc ______________
  File "/testbed/sympy/printing/tests/test_ccode.py", line 178, in test_ccode_sinc
    assert ccode(expr) == (
AssertionError

============= tests finished: 30 passed, 2 failed, in 2.39 seconds =============
"""


# --- CASE A: all tests pass ---------------------------------------------------


def test_case_a_all_passed() -> None:
    outcome = parse_test_output(SYMPY_ALL_PASS, "", 0)
    assert outcome.framework == "sympy"
    assert outcome.parsed is True
    assert (outcome.passed, outcome.failed, outcome.errors) == (27, 0, 0)
    assert outcome.total == 27
    assert outcome.green is True
    assert outcome.duration_s == pytest.approx(1.86)


# --- CASE B: failures ---------------------------------------------------------


def test_case_b_passed_and_failed() -> None:
    outcome = parse_test_output(SYMPY_TWO_FAILED, "", 1)
    assert (outcome.passed, outcome.failed) == (30, 2)
    assert outcome.green is False
    assert outcome.has_failures is True
    assert outcome.total == 32


# --- CASE C: exceptions and expected failures ---------------------------------


def test_case_c_exceptions_are_errors_not_failures() -> None:
    outcome = parse_test_output(SYMPY_XFAIL_AND_EXC, "", 1)
    assert outcome.passed == 79
    assert outcome.failed == 1
    assert outcome.errors == 1, "SymPy calls these 'exceptions'"
    assert outcome.xfail == 1
    assert outcome.green is False
    # The duration wraps onto the following line in this real sample.
    assert outcome.duration_s == pytest.approx(4.29)


# --- CASE D: singular and plural forms ----------------------------------------


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("tests finished: 1 passed, in 0.10 seconds", (1, 0, 0)),
        ("tests finished: 100 passed, in 9.00 seconds", (100, 0, 0)),
        ("tests finished: 1 passed, 1 failed, in 0.10 seconds", (1, 1, 0)),
        ("tests finished: 20 passed, 1 exception, in 0.10 seconds", (20, 0, 1)),
        ("tests finished: 20 passed, 2 exceptions, in 0.10 seconds", (20, 0, 2)),
        ("tests finished: 5 passed, 1 error, in 0.10 seconds", (5, 0, 1)),
        ("tests finished: 1 passed, in 1 second", (1, 0, 0)),
    ],
)
def test_case_d_singular_and_plural(summary: str, expected: tuple[int, int, int]) -> None:
    outcome = parse_test_output(summary, "", 0)
    assert outcome.parsed is True
    assert (outcome.passed, outcome.failed, outcome.errors) == expected


# --- CASE E: decorative padding and whitespace --------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "================== tests finished: 27 passed, in 1.86 seconds ==================",
        "= tests finished: 27 passed, in 1.86 seconds =",
        "tests finished: 27 passed, in 1.86 seconds",
        "   tests   finished:   27   passed,   in 1.86 seconds   ",
        "== tests finished: 27 passed, in 1.86 seconds ===",  # uneven padding, seen in logs
    ],
)
def test_case_e_padding_and_spacing_do_not_matter(summary: str) -> None:
    outcome = parse_test_output(summary, "", 0)
    assert outcome.parsed is True
    assert outcome.passed == 27


# --- CASE F: surrounding output -----------------------------------------------


def test_case_f_summary_found_among_other_output() -> None:
    noisy = (
        "+ source activate testbed\n"
        "sympy/printing/tests/test_ccode.py[30] ..............................  [OK]\n"
        "\n" + SYMPY_ALL_PASS + "\n"
        "DO *NOT* COMMIT!\n"
        "+ SWEBENCH_TEST_EXIT_CODE=0\n"
    )
    outcome = parse_test_output(noisy, "", 0)
    assert outcome.parsed is True
    assert outcome.passed == 27
    assert outcome.green is True


def test_case_f_stderr_is_searched_too() -> None:
    outcome = parse_test_output("", SYMPY_TWO_FAILED, 1)
    assert outcome.parsed is True
    assert outcome.failed == 2


# --- CASE G: failing test names -----------------------------------------------


def test_case_g_failing_test_names_are_extracted() -> None:
    outcome = parse_test_output(SYMPY_FAILURE_BANNERS, "", 1)
    assert outcome.failing_tests == ["test_ccode_Relational", "test_ccode_sinc"]
    assert outcome.passed == 30
    assert outcome.failed == 2
    assert outcome.green is False


def test_case_g_no_names_are_invented_when_absent() -> None:
    """Only names the output actually contains may be reported."""
    outcome = parse_test_output(SYMPY_TWO_FAILED, "", 1)
    assert outcome.failed == 2
    assert outcome.failing_tests == [], "two failures, but the output names none"


def test_case_g_names_are_deduplicated() -> None:
    doubled = SYMPY_FAILURE_BANNERS + SYMPY_FAILURE_BANNERS
    assert parse_test_output(doubled, "", 1).failing_tests == [
        "test_ccode_Relational",
        "test_ccode_sinc",
    ]


# --- CASE H: exit code disagrees with the summary -----------------------------


def test_case_h_nonzero_exit_with_a_passing_summary() -> None:
    """The summary is the stronger evidence, but a green claim still needs care.

    SymPy exits non-zero whenever anything went wrong, including exceptions it
    also reports in the summary. When the summary itself is clean we trust it --
    that is the whole reason for parsing rather than reading exit codes.
    """
    outcome = parse_test_output(SYMPY_ALL_PASS, "", 1)
    assert outcome.parsed is True
    assert outcome.passed == 27
    assert outcome.green is True, "a parsed, clean summary is the authority"


def test_case_h_a_timeout_is_never_green() -> None:
    outcome = parse_test_output(SYMPY_ALL_PASS, "", 0, timed_out=True)
    assert outcome.green is False


# --- CASE I: unrecognised output must not look verified -----------------------


def test_case_i_unparsed_output_is_not_confident_verification() -> None:
    outcome = parse_test_output("some tool printed nothing useful\n", "", 0)
    assert outcome.parsed is False
    assert outcome.framework == "unknown"
    assert (outcome.passed, outcome.failed) == (0, 0)
    # `green` still falls back to the exit code, which is how unsupported
    # runners stay usable -- but `parsed` is what callers must check before
    # treating a run as verification.
    assert outcome.green is True
    assert "no recognised test summary" in outcome.describe()


def test_case_i_a_timed_out_sympy_run_has_no_summary() -> None:
    """Real case: one instance hung and the harness killed it at 1800s."""
    partial = "test_sin_cos\n\nTimeout error: 1800 seconds exceeded.\n"
    outcome = parse_test_output(partial, "", 1, timed_out=True)
    assert outcome.parsed is False
    assert outcome.green is False


# --- CASE J: pytest must be untouched -----------------------------------------


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("===== 12 passed in 0.50s =====", (12, 0, 0, 0)),
        ("===== 12 passed, 1 skipped in 0.50s =====", (12, 0, 0, 1)),
        ("===== 12 passed, 1 failed in 0.50s =====", (12, 1, 0, 0)),
        ("2 failed, 183 passed, 3 skipped in 12.34s", (183, 2, 0, 3)),
        ("===== 1 error in 0.10s =====", (0, 0, 1, 0)),
        ("===== 3 passed, 2 errors in 1.00s =====", (3, 0, 2, 0)),
    ],
)
def test_case_j_pytest_still_parses(summary: str, expected: tuple[int, int, int, int]) -> None:
    outcome = parse_test_output(summary, "", 1)
    assert outcome.framework == "pytest"
    assert outcome.parsed is True
    assert (outcome.passed, outcome.failed, outcome.errors, outcome.skipped) == expected


def test_case_j_pytest_failing_names_still_extracted() -> None:
    text = (
        "FAILED tests/test_a.py::test_one - AssertionError\n"
        "ERROR tests/test_b.py::test_two\n"
        "===== 1 failed, 1 error, 3 passed in 0.50s =====\n"
    )
    outcome = parse_test_output(text, "", 1)
    assert outcome.framework == "pytest"
    assert outcome.failing_tests == ["tests/test_a.py::test_one", "tests/test_b.py::test_two"]


def test_case_j_pytest_no_tests_ran_still_handled() -> None:
    outcome = parse_test_output("===== no tests ran in 0.01s =====", "", 5)
    assert outcome.framework == "pytest"
    assert outcome.parsed is True
    assert outcome.green is False, "nothing ran, so nothing was verified"


def test_case_j_unittest_and_jest_unaffected() -> None:
    unittest_out = "Ran 12 tests in 0.501s\n\nFAILED (failures=2, errors=1)\n"
    outcome = parse_test_output(unittest_out, "", 1)
    assert outcome.framework == "unittest"
    assert (outcome.total, outcome.failed, outcome.errors) == (12, 2, 1)

    jest_out = "Tests:       2 failed, 10 passed, 12 total\n"
    outcome = parse_test_output(jest_out, "", 1)
    assert outcome.framework == "jest"
    assert (outcome.passed, outcome.failed, outcome.total) == (10, 2, 12)


# --- CASE K: expected failures are not failures -------------------------------


def test_case_k_expected_failures_alone_stay_green() -> None:
    outcome = parse_test_output(SYMPY_SKIPPED_XFAIL, "", 0)
    assert outcome.passed == 83
    assert outcome.skipped == 1
    assert outcome.xfail == 1
    assert outcome.failed == 0 and outcome.errors == 0
    assert outcome.green is True, "an expected failure is the suite working as intended"


def test_case_k_exceptions_alongside_xfail_are_still_red() -> None:
    outcome = parse_test_output(SYMPY_EXCEPTIONS_ONLY, "", 1)
    assert outcome.passed == 37
    assert outcome.xfail == 2
    assert outcome.errors == 9
    assert outcome.green is False


# --- CASE L: error and exception wording --------------------------------------


@pytest.mark.parametrize(
    "phrase",
    ["9 exceptions", "1 exceptions", "1 exception", "2 errors", "1 error"],
)
def test_case_l_error_wordings_all_count_as_errors(phrase: str) -> None:
    outcome = parse_test_output(f"tests finished: 5 passed, {phrase}, in 1.00 seconds", "", 1)
    assert outcome.errors == int(phrase.split()[0])
    assert outcome.green is False


# --- overfitting guards --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The build passed and 30 tests were mentioned in the README.",
        "42 passed the code review",
        "tests finished:",  # anchor present, no counts
        "tests finished: in 1.00 seconds",
    ],
)
def test_prose_containing_passed_is_not_mistaken_for_a_summary(text: str) -> None:
    outcome = parse_test_output(text, "", 0)
    assert outcome.framework != "sympy"
    assert outcome.parsed is False


def test_the_last_summary_wins_when_several_are_present() -> None:
    """Agents often run one command that produces several suite summaries."""
    text = SYMPY_ALL_PASS + "\n" + SYMPY_TWO_FAILED
    outcome = parse_test_output(text, "", 1)
    assert (outcome.passed, outcome.failed) == (30, 2)


def test_describe_reports_every_recognised_category() -> None:
    outcome = parse_test_output(SYMPY_XFAIL_AND_EXC, "", 1)
    described = outcome.describe()
    for fragment in ("79 passed", "1 failed", "1 errors", "1 expected to fail"):
        assert fragment in described


def test_failure_excerpt_anchors_on_the_sympy_banner() -> None:
    noise = "ok\n" * 500
    outcome = extract_failure_excerpt(noise + SYMPY_FAILURE_BANNERS, "")
    assert "test_ccode_Relational" in outcome
    assert outcome.count("ok\n") < 100, "the passing noise should be dropped"


def test_outcome_defaults_are_not_a_verified_state() -> None:
    blank = Outcome()
    assert blank.parsed is False
    assert blank.has_failures is False
    assert blank.green is True, "exit 0 by default; `parsed` is the real gate"


# --- A run long enough for pytest to print an elapsed time --------------------
#
# Found in production, not imagined: a jmespath publication ran the project's
# hypothesis fuzzing suite, which took seven minutes and passed. Because pytest
# appends "(0:07:06)" once a run reaches a minute, the summary line stopped
# matching and 998 green tests were recorded as `framework=unknown, passed=0`.
# The agent was told its tests produced nothing and spent iterations re-running
# them. Slow suites are exactly where this must not happen.

REAL_JMESPATH_TAIL = """\
tests/test_lexer.py: 3 warnings
  /workspace/jmespath/lexer.py:169: PendingDeprecationWarning: deprecated string literal syntax
    warnings.warn("deprecated string literal syntax",

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
998 passed, 1 skipped, 18 warnings in 426.92s (0:07:06)
"""


def test_a_long_run_with_an_elapsed_suffix_is_recognised() -> None:
    outcome = parse_test_output(REAL_JMESPATH_TAIL, "", 0)
    assert outcome.framework == "pytest"
    assert outcome.parsed is True
    assert (outcome.passed, outcome.failed, outcome.skipped) == (998, 0, 1)
    assert outcome.green is True


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("998 passed, 1 skipped in 426.92s (0:07:06)", (998, 0, 1)),
        ("998 passed, 1 skipped, 18 warnings in 426.92s (0:07:06)", (998, 0, 1)),
        ("===== 2 failed, 183 passed in 120.00s (0:02:00) =====", (183, 2, 0)),
        ("1 failed, 4 passed in 3601.00s (1:00:01)", (4, 1, 0)),
        ("5 passed in 90000.00s (1 day, 1:00:00)", (5, 0, 0)),
    ],
)
def test_the_elapsed_suffix_never_hides_the_counts(
    summary: str, expected: tuple[int, int, int]
) -> None:
    outcome = parse_test_output(summary, "", 0)
    assert outcome.framework == "pytest"
    assert outcome.parsed is True
    assert (outcome.passed, outcome.failed, outcome.skipped) == expected


def test_no_tests_ran_still_parses_with_an_elapsed_suffix() -> None:
    outcome = parse_test_output("no tests ran in 61.00s (0:01:01)", "", 5)
    assert outcome.framework == "pytest"
    assert outcome.parsed is True
    assert outcome.passed == 0
