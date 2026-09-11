"""Repair progress: how close is this run to an actual source fix?

Two benchmark passes produced the evidence this module encodes.

**The first pass** showed six of fifteen runs reaching the iteration cap without
editing anything, while the anti-loop detector stayed silent -- every action was
new, so nothing looked like a loop. What nobody was watching was the arc.

**The second pass** showed why those runs never edited, and it is not that they
failed to understand the bug. All six named the correct source file in their
final summary, and five named the exact function. What separated them from the
runs that produced a patch was the order of operations::

    verdict         reproductions BEFORE first edit    reproductions AFTER
    EMPTY_PATCH        10, 7, 5, 2, 1, 0                      none
    patched             none                                2 to 7

The correlation is total. A run that starts by building a reproduction gets
absorbed by it: each run reveals another detail, which invites another run, and
the budget goes. A run that reads code, edits, and *then* reproduces uses the
reproduction as verification and ships a patch.

So reproduction is a verification instrument, not a localisation one, and
"reproduced repeatedly but never edited" is a runtime-detectable predictor of an
empty patch. That is the signal this module exists to raise.

It measures and advises. It never edits and never blocks an edit: forcing a
change at a fixed iteration would trade six no-patch runs for six random-patch
runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: Tools that write to the working tree.
WRITE_TOOLS: frozenset[str] = frozenset({"apply_patch", "edit_file", "write_file"})

#: Tools that gather evidence without changing anything.
READ_TOOLS: frozenset[str] = frozenset(
    {"read_file", "search_code", "find_symbol", "list_files", "git_log", "git_diff"}
)

#: Scratch space PatchPilot provides. Excluded from the patch by the workspace.
SCRATCH_PREFIXES: tuple[str, ...] = (".patchpilot/", ".patchpilot")

#: Root-level files whose names read as a throwaway script rather than a fix.
SCRATCH_FILE = re.compile(
    r"^(repro|reproduce|debug|scratch|tmp|temp|snippet|example|check)[\w.-]*\.py$",
    re.IGNORECASE,
)

#: A project's own tests. Editing these is legitimate work but is not the fix.
TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]+$|_test\.py$", re.IGNORECASE)


class WriteKind(StrEnum):
    """What a write to the working tree actually was."""

    SCRATCH = "scratch"
    """A reproduction or debug script. Never part of the submitted patch."""
    TEST = "test"
    """A change to the project's own tests."""
    SOURCE = "source"
    """A change to implementation code -- the only kind that can fix anything."""


def classify_write(path: str) -> WriteKind:
    """Decide what a written path is, from the path alone."""
    normalised = path.replace(chr(92), "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]  # prefix strip; lstrip("./") would eat the dot
    if normalised.startswith(SCRATCH_PREFIXES):
        return WriteKind.SCRATCH
    if "/" not in normalised and SCRATCH_FILE.match(normalised):
        return WriteKind.SCRATCH
    if TEST_PATH.search(normalised):
        return WriteKind.TEST
    return WriteKind.SOURCE


def is_scratch(path: str) -> bool:
    """True when a written path is scratch space rather than a candidate fix."""
    return classify_write(path) is WriteKind.SCRATCH


def refuse_test_writes(path: str) -> str | None:
    """Reason to refuse a write to ``path``, or None to allow it.

    Used where the tests are the answer key rather than the agent's to change --
    a benchmark, where FAIL_TO_PASS and PASS_TO_PASS come from the harness. An
    agent that may edit them can turn any run green without fixing anything, and
    a score built on that measures nothing.

    Deliberately not the default. On a real repository, adding a regression test
    beside a fix is exactly what a good contributor does; there the edit is
    surfaced to the reviewer instead of being blocked.
    """
    if classify_write(path) is not WriteKind.TEST:
        return None
    return (
        f"refusing to modify {path}: the tests define the behaviour this run is "
        "judged against and cannot be changed from inside it. Fix the "
        "implementation instead."
    )


#: A command that exercises the project's own behaviour: an inline snippet, or a
#: script from the scratch directory. This is what "reproducing" looks like in
#: the trajectories -- `python -c "from sympy import ...; print(...)"`.
_INLINE_SNIPPET = re.compile(r"(^|\s)-c(\s|$)")
_IMPORTS = re.compile(r"\b(import|from)\s+\w")


def looks_like_reproduction(command: str) -> bool:
    """True when a command runs project code to observe its behaviour."""
    if _INLINE_SNIPPET.search(command) and _IMPORTS.search(command):
        return True
    return ".patchpilot/" in command.replace(chr(92), "/")


class Pressure(StrEnum):
    """How overdue a source edit is."""

    NONE = "none"
    """Still localising, or a source edit already exists."""
    NUDGE = "nudge"
    """Enough is known to attempt a repair; exploring further needs a reason."""
    URGENT = "urgent"
    """The run will produce nothing unless it edits implementation code now."""


@dataclass
class ProgressTracker:
    """Milestones, staleness and repair state for one run.

    Iteration numbers are recorded the first time each milestone happens and are
    ``None`` until then. That distinction is the point: a
    ``time_to_first_source_edit`` of ``None`` means *never touched implementation
    code*, which is a different and much worse failure than editing late.
    """

    #: Fraction of the iteration budget after which an un-started repair is worth
    #: a nudge, then a warning. Successful runs made their first source edit at
    #: iteration 2-8 of 22 (9%-36%), so a run still exploring at 45% is already
    #: an outlier. These are the *fallback* triggers; the evidence-based ones
    #: below usually fire earlier.
    nudge_at: float = 0.45
    urgent_at: float = 0.70

    #: Reproductions before any source edit, past which the run is behaving like
    #: one of the six empty-patch trajectories. Those ran 1, 2, 5, 7 and 10.
    repro_without_edit_limit: int = 2

    #: Times the same file or query may be re-examined before it has stopped
    #: yielding anything. Successful runs peaked at 1-2; failures reached 5-7.
    repeat_inspection_limit: int = 3

    # --- milestones ----------------------------------------------------------
    first_file_read: int | None = None
    first_hypothesis: int | None = None
    first_any_write: int | None = None
    first_scratch_write: int | None = None
    first_test_edit: int | None = None
    first_source_edit: int | None = None
    first_test_run: int | None = None
    first_green_test: int | None = None
    first_behaviour_check: int | None = None
    """First reproduction run *after* a source edit -- the verification step
    every resolved run performed and no empty-patch run reached."""

    # --- counters ------------------------------------------------------------
    source_edits: int = 0
    scratch_writes: int = 0
    test_edits: int = 0
    reads: int = 0
    reproductions: int = 0
    reproductions_before_edit: int = 0
    iterations_since_source_edit: int = 0

    files_seen: set[str] = field(default_factory=set)
    source_files_touched: set[str] = field(default_factory=set)
    candidate_files: list[str] = field(default_factory=list)
    """Source files read, in order -- the places a repair could go."""
    inspections: dict[str, int] = field(default_factory=dict)
    """How often each read target has been examined."""

    _warned: set[str] = field(default_factory=set)

    # --- recording -----------------------------------------------------------
    def record_tool(
        self, tool: str, arguments: dict[str, object], iteration: int, *, succeeded: bool
    ) -> None:
        """Note one tool call. Only successful calls count as progress."""
        if not succeeded:
            return

        if tool in WRITE_TOOLS:
            self._record_write(arguments, iteration)
        elif tool in READ_TOOLS:
            self._record_read(tool, arguments, iteration)
        elif tool == "record_hypothesis" and self.first_hypothesis is None:
            self.first_hypothesis = iteration
        elif tool == "run_command":
            command = str(arguments.get("command") or "")
            if looks_like_reproduction(command):
                self._record_reproduction(iteration)

    def _record_write(self, arguments: dict[str, object], iteration: int) -> None:
        raw = arguments.get("path") or arguments.get("file_path")
        # apply_patch carries a diff rather than a path; without a path to judge,
        # assume source -- the workspace still keeps scratch out of the patch.
        path = str(raw) if isinstance(raw, str) and raw else ""
        kind = classify_write(path) if path else WriteKind.SOURCE

        if self.first_any_write is None:
            self.first_any_write = iteration
        if kind is WriteKind.SCRATCH:
            self.scratch_writes += 1
            if self.first_scratch_write is None:
                self.first_scratch_write = iteration
            return  # a reproduction script is not a step toward the fix
        if kind is WriteKind.TEST:
            self.test_edits += 1
            if self.first_test_edit is None:
                self.first_test_edit = iteration
            return  # editing tests is real work, but it is not the repair

        self.source_edits += 1
        self.iterations_since_source_edit = 0
        if path:
            self.source_files_touched.add(path)
        if self.first_source_edit is None:
            self.first_source_edit = iteration

    def _record_read(self, tool: str, arguments: dict[str, object], iteration: int) -> None:
        self.reads += 1
        target = arguments.get("path") or arguments.get("query") or arguments.get("symbol")
        if not isinstance(target, str) or not target:
            return
        key = f"{tool}:{target}"
        self.inspections[key] = self.inspections.get(key, 0) + 1
        if target not in self.files_seen:
            self.files_seen.add(target)
            if tool == "read_file":
                self.candidate_files.append(target)
        if tool == "read_file" and self.first_file_read is None:
            self.first_file_read = iteration

    def _record_reproduction(self, iteration: int) -> None:
        self.reproductions += 1
        if self.has_source_edit:
            if self.first_behaviour_check is None:
                self.first_behaviour_check = iteration
        else:
            self.reproductions_before_edit += 1

    def record_test(self, iteration: int, *, green: bool) -> None:
        if self.first_test_run is None:
            self.first_test_run = iteration
        if green and self.first_green_test is None:
            self.first_green_test = iteration

    def end_iteration(self, *, mutated: bool, saw_new_file: bool = False) -> None:
        # `mutated` is any working-tree change, but only a *source* change resets
        # the staleness clock, which is why this does not simply mirror it.
        del saw_new_file
        if mutated and self.has_source_edit:
            self.iterations_since_source_edit = 0
        else:
            self.iterations_since_source_edit += 1

    # --- repair state --------------------------------------------------------
    @property
    def has_source_edit(self) -> bool:
        return self.source_edits > 0

    @property
    def has_edit(self) -> bool:
        """Only a source edit counts as an edit toward a fix."""
        return self.has_source_edit

    @property
    def localised(self) -> bool:
        """True once the run has read implementation code it could repair."""
        return bool(self.candidate_files)

    @property
    def has_diagnosis(self) -> bool:
        """Enough is known to attempt a repair.

        Deliberately behavioural rather than declarative. `record_hypothesis`
        was called in only 7 of 20 recorded runs, so requiring it would leave
        the orchestrator blind most of the time. Having read the implementation
        *and* observed the failing behaviour is the same evidence, and it is
        what the agent actually does.
        """
        if self.first_hypothesis is not None:
            return True
        return self.localised and self.reproductions > 0

    @property
    def behaviour_checked(self) -> bool:
        """A reproduction was run after a source edit."""
        return self.first_behaviour_check is not None

    def over_inspected(self) -> str | None:
        """The read target examined past the point of yielding anything new."""
        for key, count in self.inspections.items():
            if count > self.repeat_inspection_limit:
                return key.split(":", 1)[1]
        return None

    # --- decisions -----------------------------------------------------------
    def pressure(self, iteration: int, max_iterations: int) -> Pressure:
        """How overdue a source edit is, given the evidence and the budget.

        Evidence first, clock second. A run that has localised the
        implementation and reproduced the failure repeatedly already knows
        enough to try something, whether that is iteration 6 or 16 -- and
        waiting on a budget fraction is exactly what let the empty-patch runs
        reproduce ten times before running out.
        """
        if self.has_source_edit or max_iterations <= 0:
            return Pressure.NONE

        spent = iteration / max_iterations
        if spent >= self.urgent_at:
            return Pressure.URGENT
        # The empty-patch signature: reproducing the failure over and over with
        # the implementation already open.
        if self.localised and self.reproductions_before_edit > self.repro_without_edit_limit:
            return Pressure.URGENT
        if spent >= self.nudge_at:
            return Pressure.NUDGE
        if self.has_diagnosis:
            return Pressure.NUDGE
        return Pressure.NONE

    def should_warn(self, key: str) -> bool:
        """True once per distinct escalation, so advice never becomes noise."""
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    def best_candidate(self) -> str | None:
        """The most recently read source file: the likeliest place for a repair."""
        return self.candidate_files[-1] if self.candidate_files else None

    # --- reporting -----------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        """Diagnostics recorded on the run, so trajectories stay comparable."""
        return {
            "time_to_first_source_edit": self.first_source_edit,
            "time_to_first_any_write": self.first_any_write,
            "time_to_first_scratch_write": self.first_scratch_write,
            "time_to_first_test_edit": self.first_test_edit,
            "time_to_first_file_read": self.first_file_read,
            "time_to_first_hypothesis": self.first_hypothesis,
            "time_to_first_test": self.first_test_run,
            "time_to_first_green_test": self.first_green_test,
            "time_to_first_behaviour_check": self.first_behaviour_check,
            "source_edits": self.source_edits,
            "scratch_writes": self.scratch_writes,
            "test_edits": self.test_edits,
            "reads": self.reads,
            "reproductions": self.reproductions,
            "reproductions_before_edit": self.reproductions_before_edit,
            "files_seen": len(self.files_seen),
            "localised": self.localised,
            "has_diagnosis": self.has_diagnosis,
        }
