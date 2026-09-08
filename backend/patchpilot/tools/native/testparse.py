"""Test-output parsing.

The agent's most important observation is "did the tests pass, and which ones
failed". Exit codes alone are not enough — a collection error and 200 failures
both exit non-zero — so we parse the runner's own summary.

Supported: pytest, unittest, and (best-effort) jest/mocha. Anything else falls
back to the exit code, which is still a usable signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# pytest's final summary line, in both of the forms it actually emits:
#   "===== 2 failed, 183 passed, 3 skipped in 12.34s ====="   (default)
#   "2 failed, 183 passed in 12.34s"                          (-q / --quiet)
# The undecorated form is the one that matters most in practice, because an
# agent almost always runs pytest quietly.
_COUNT_WORD = r"passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun"
_PYTEST_SUMMARY = re.compile(
    rf"^[=\s]*(?P<body>(?:\d+\s+(?:{_COUNT_WORD})(?:\s*,\s*)?)+(?:\s*in\s+[\d.]+\s*s\S*)?)[=\s]*$"
    rf"|^[=\s]*(?P<empty>no tests ran(?:\s+in\s+[\d.]+\s*s\S*)?)[=\s]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PYTEST_COUNT = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected)")
_PYTEST_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)
_PYTEST_COLLECTED = re.compile(r"collected (\d+) items?")

# "Ran 12 tests in 0.501s" / "FAILED (failures=2, errors=1)"
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.MULTILINE)
_UNITTEST_FAILED = re.compile(r"^FAILED \((.*)\)", re.MULTILINE)
_UNITTEST_COUNT = re.compile(r"(failures|errors|skipped|expected failures)=(\d+)")

# jest: "Tests:       2 failed, 10 passed, 12 total"
_JEST_TESTS = re.compile(r"^Tests:\s+(.*)$", re.MULTILINE)
_JEST_COUNT = re.compile(r"(\d+)\s+(failed|passed|skipped|todo|total)")


@dataclass(slots=True)
class TestOutcome:
    """Normalised result of one test command."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    total: int = 0
    duration_s: float = 0.0
    exit_code: int = 0
    timed_out: bool = False
    framework: str = "unknown"
    parsed: bool = False
    failing_tests: list[str] = field(default_factory=list)
    summary_line: str = ""

    @property
    def green(self) -> bool:
        """True only when the runner clearly reported success."""
        if self.timed_out:
            return False
        if self.parsed:
            return self.failed == 0 and self.errors == 0 and (self.passed > 0 or self.total > 0)
        return self.exit_code == 0

    @property
    def has_failures(self) -> bool:
        return self.failed > 0 or self.errors > 0

    def describe(self) -> str:
        if self.timed_out:
            return "test run TIMED OUT"
        if not self.parsed:
            return f"exit code {self.exit_code} (no recognised test summary)"
        bits = [f"{self.passed} passed"]
        if self.failed:
            bits.append(f"{self.failed} failed")
        if self.errors:
            bits.append(f"{self.errors} errors")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        return ", ".join(bits)


def parse_test_output(
    stdout: str, stderr: str, exit_code: int, *, timed_out: bool = False
) -> TestOutcome:
    """Parse a test run's output into a :class:`TestOutcome`."""
    text = f"{stdout}\n{stderr}"
    outcome = TestOutcome(exit_code=exit_code, timed_out=timed_out)

    if _parse_pytest(text, outcome):
        return outcome
    if _parse_unittest(text, outcome):
        return outcome
    if _parse_jest(text, outcome):
        return outcome
    return outcome


def _parse_pytest(text: str, outcome: TestOutcome) -> bool:
    matches = list(_PYTEST_SUMMARY.finditer(text))
    if not matches:
        return False
    last = matches[-1]
    body = (last.group("body") or last.group("empty") or "").strip()
    if not body:
        return False
    counts = _PYTEST_COUNT.findall(body.lower())
    if not counts and "no tests ran" not in body.lower():
        return False

    outcome.framework = "pytest"
    outcome.parsed = True
    outcome.summary_line = body.strip()
    for value, label in counts:
        number = int(value)
        if label == "passed":
            outcome.passed = number
        elif label == "failed":
            outcome.failed = number
        elif label.startswith("error"):
            outcome.errors = number
        elif label == "skipped":
            outcome.skipped = number

    duration = re.search(r"in\s+([\d.]+)\s*s", body)
    if duration:
        outcome.duration_s = float(duration.group(1))

    collected = _PYTEST_COLLECTED.search(text)
    outcome.total = (
        int(collected.group(1))
        if collected
        else outcome.passed + outcome.failed + outcome.errors + outcome.skipped
    )
    outcome.failing_tests = _unique(_PYTEST_FAILED_LINE.findall(text))[:50]
    return True


def _parse_unittest(text: str, outcome: TestOutcome) -> bool:
    ran = _UNITTEST_RAN.search(text)
    if not ran:
        return False
    outcome.framework = "unittest"
    outcome.parsed = True
    outcome.total = int(ran.group(1))
    outcome.duration_s = float(ran.group(2))

    failed_block = _UNITTEST_FAILED.search(text)
    if failed_block:
        outcome.summary_line = f"FAILED ({failed_block.group(1)})"
        for label, value in _UNITTEST_COUNT.findall(failed_block.group(1)):
            number = int(value)
            if label == "failures":
                outcome.failed = number
            elif label == "errors":
                outcome.errors = number
            elif label == "skipped":
                outcome.skipped = number
    else:
        outcome.summary_line = f"OK ({outcome.total} tests)"

    outcome.passed = max(0, outcome.total - outcome.failed - outcome.errors - outcome.skipped)
    outcome.failing_tests = _unique(
        re.findall(r"^(?:FAIL|ERROR):\s+(\S+)", text, re.MULTILINE)
    )[:50]
    return True


def _parse_jest(text: str, outcome: TestOutcome) -> bool:
    line = _JEST_TESTS.search(text)
    if not line:
        return False
    outcome.framework = "jest"
    outcome.parsed = True
    outcome.summary_line = line.group(1).strip()
    for value, label in _JEST_COUNT.findall(line.group(1)):
        number = int(value)
        if label == "passed":
            outcome.passed = number
        elif label == "failed":
            outcome.failed = number
        elif label == "skipped":
            outcome.skipped = number
        elif label == "total":
            outcome.total = number
    return True


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def extract_failure_excerpt(stdout: str, stderr: str, limit: int = 6000) -> str:
    """Pull the most informative part of a failing test run.

    Test output is dominated by passing-test noise; the agent needs the failure
    section. We anchor on pytest's ``FAILURES`` banner when present and fall
    back to the tail of the output.
    """
    text = stdout if stdout.strip() else stderr
    if not text.strip():
        text = stderr

    for marker in ("=== FAILURES ===", "= FAILURES =", "=== ERRORS ===", "= ERRORS ="):
        index = text.find(marker)
        if index != -1:
            excerpt = text[index:]
            return excerpt[:limit] + ("\n...[truncated]" if len(excerpt) > limit else "")

    for marker in ("Traceback (most recent call last)", "FAIL:", "ERROR:"):
        index = text.find(marker)
        if index != -1:
            excerpt = text[max(0, index - 200) :]
            return excerpt[:limit] + ("\n...[truncated]" if len(excerpt) > limit else "")

    if len(text) <= limit:
        return text
    return "...[head truncated]\n" + text[-limit:]
