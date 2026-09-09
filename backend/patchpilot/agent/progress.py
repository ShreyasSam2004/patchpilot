"""Progress tracking: does this run know more than it did five iterations ago?

The 15-instance SWE-bench batch produced six runs that reached the iteration
cap without ever editing a file. Reading their trajectories, none of them was
stuck in an obvious repetition -- the anti-loop detector fired once, at
iteration 4, and then stayed silent for eighteen more. Each individual action
looked reasonable; what nobody was watching was the *arc*: twenty-two turns of
investigation and not one edit.

This module makes that arc a first-class measurement. It answers two questions
the loop could not previously ask:

* **Where did the time go?**  ``time_to_first_edit`` and its siblings turn "the
  agent explored too long" into a number that can be compared across runs.
* **Should we still be exploring?**  :meth:`ProgressTracker.pressure` reports
  how overdue an edit is, given how much budget is left and whether the run has
  a candidate location to edit at all.

It deliberately does not decide anything. The orchestrator reads it and injects
advice; nothing here can force an edit, because forcing a random edit at a fixed
iteration is worse than the problem it solves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Tools that constitute an edit to the repository.
EDIT_TOOLS: frozenset[str] = frozenset({"apply_patch", "edit_file", "write_file"})

#: Paths that are scratch space, not a fix. Writing a reproduction script is
#: useful work, but it is not progress toward a patch -- and counting it as one
#: silenced the anti-paralysis nudge for a run whose entire final "patch" turned
#: out to be a debug script.
SCRATCH_PREFIXES: tuple[str, ...] = (".patchpilot/", ".patchpilot")
SCRATCH_FILE = re.compile(
    r"^(repro|reproduce|debug|scratch|tmp|temp|snippet|example|check)[\w.-]*\.py$",
    re.IGNORECASE,
)


def is_scratch(path: str) -> bool:
    """True when a written path is scratch space rather than a candidate fix."""
    normalised = path.replace(chr(92), "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]  # a prefix strip; lstrip("./") eats the dot
    if normalised.startswith(SCRATCH_PREFIXES):
        return True
    return "/" not in normalised and bool(SCRATCH_FILE.match(normalised))

#: Tools that gather evidence without changing anything.
READ_TOOLS: frozenset[str] = frozenset(
    {"read_file", "search_code", "find_symbol", "list_files", "git_log", "git_diff"}
)


class Pressure(StrEnum):
    """How overdue a first edit is."""

    NONE = "none"
    """Still early, or an edit already exists."""
    NUDGE = "nudge"
    """Enough of the budget is gone that exploration should be converging."""
    URGENT = "urgent"
    """Most of the budget is gone and nothing has been changed yet."""


@dataclass
class ProgressTracker:
    """Milestones and staleness for one run.

    Iteration numbers are recorded the first time each milestone happens, and
    are ``None`` while it has not. That distinction matters for reporting: a
    ``time_to_first_edit`` of ``None`` is "never edited", which is a different
    failure from "edited late".
    """

    #: Fraction of the iteration budget after which an un-started patch is worth
    #: a nudge, then a warning. Tuned from the batch: successful runs made their
    #: first edit at iteration 2-8 of 22 (9%-36%), so a run still exploring at
    #: 45% is already an outlier, and one at 70% has essentially failed.
    nudge_at: float = 0.45
    urgent_at: float = 0.70

    first_file_read: int | None = None
    first_hypothesis: int | None = None
    first_edit: int | None = None
    first_test_run: int | None = None
    first_green_test: int | None = None

    edits: int = 0
    reads: int = 0
    iterations_since_edit: int = 0
    iterations_since_new_file: int = 0

    files_seen: set[str] = field(default_factory=set)
    candidate_files: list[str] = field(default_factory=list)
    """Files the run has actually read, in order -- the places an edit could go."""

    _nudged: set[Pressure] = field(default_factory=set)

    # ------------------------------------------------------------- recording
    def record_tool(
        self, tool: str, arguments: dict[str, object], iteration: int, *, succeeded: bool
    ) -> None:
        """Note one tool call. Only successful calls count as progress."""
        if not succeeded:
            return
        if tool in EDIT_TOOLS:
            path = arguments.get("path") or arguments.get("file_path")
            if isinstance(path, str) and is_scratch(path):
                return  # a reproduction script is not a step toward the fix
            self.edits += 1
            self.iterations_since_edit = 0
            if self.first_edit is None:
                self.first_edit = iteration
        elif tool in READ_TOOLS:
            self.reads += 1
            path = arguments.get("path") or arguments.get("file_path")
            if isinstance(path, str) and path:
                if path not in self.files_seen:
                    self.files_seen.add(path)
                    self.iterations_since_new_file = 0
                    if tool == "read_file":
                        self.candidate_files.append(path)
                if self.first_file_read is None and tool == "read_file":
                    self.first_file_read = iteration
        elif tool == "record_hypothesis" and self.first_hypothesis is None:
            self.first_hypothesis = iteration

    def record_test(self, iteration: int, *, green: bool) -> None:
        if self.first_test_run is None:
            self.first_test_run = iteration
        if green and self.first_green_test is None:
            self.first_green_test = iteration

    def end_iteration(self, *, mutated: bool, saw_new_file: bool) -> None:
        self.iterations_since_edit = 0 if mutated else self.iterations_since_edit + 1
        self.iterations_since_new_file = 0 if saw_new_file else self.iterations_since_new_file + 1

    # ------------------------------------------------------------- reporting
    @property
    def has_edit(self) -> bool:
        return self.edits > 0

    def pressure(self, iteration: int, max_iterations: int) -> Pressure:
        """How overdue a first edit is, given the budget consumed so far."""
        if self.has_edit or max_iterations <= 0:
            return Pressure.NONE
        spent = iteration / max_iterations
        if spent >= self.urgent_at:
            return Pressure.URGENT
        if spent >= self.nudge_at:
            return Pressure.NUDGE
        return Pressure.NONE

    def should_warn(self, pressure: Pressure) -> bool:
        """True once per escalation, so advice arrives without becoming noise."""
        if pressure is Pressure.NONE or pressure in self._nudged:
            return False
        self._nudged.add(pressure)
        return True

    def best_candidate(self) -> str | None:
        """The most recently read file: the likeliest place for a first edit."""
        return self.candidate_files[-1] if self.candidate_files else None

    def snapshot(self) -> dict[str, object]:
        """Diagnostics recorded on the run, so trajectories stay comparable."""
        return {
            "time_to_first_edit": self.first_edit,
            "time_to_first_file_read": self.first_file_read,
            "time_to_first_hypothesis": self.first_hypothesis,
            "time_to_first_test": self.first_test_run,
            "time_to_first_green_test": self.first_green_test,
            "edits": self.edits,
            "reads": self.reads,
            "files_seen": len(self.files_seen),
            "iterations_since_edit": self.iterations_since_edit,
        }
