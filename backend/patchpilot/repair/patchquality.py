"""Is this diff an attempted repair, or just the debris of investigating one?

Evidence. In the second benchmark pass one instance submitted a patch whose
entire content was::

    diff --git a/repro_debug.py b/repro_debug.py
    new file mode 100644
    +import sympy
    +print("hash eq=%r" % (hash(ea)==hash(eb)))

Zero source changes. It was recorded as "patch generated" and graded, of course,
as unresolved. A second instance shipped `repro_test2.py` alongside a real fix.

The workspace already keeps scratch files out of the submitted diff, so that
particular patch would now simply be empty. This module answers the question the
exclusion raises: *given whatever the diff does contain, has an actual repair
been attempted?* An empty diff and a diff full of print statements are both
"no", and the run should be told so while it still has budget to act.

Classification is textual and cheap, because it runs on every finish attempt and
at the end of every run. It is deliberately conservative: anything it cannot
confidently call debris is treated as a real repair, since suppressing a genuine
fix is far worse than tolerating a scruffy one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from .progress import WriteKind, classify_write


class PatchQuality(StrEnum):
    """What the working-tree diff amounts to."""

    NO_PATCH = "no_patch"
    """Nothing changed at all."""
    SCRATCH_ONLY = "scratch_only"
    """Only reproduction or debug artefacts. Not a repair."""
    DEBUG_ONLY = "debug_only"
    """Source files touched, but only to add prints or logging."""
    TESTS_ONLY = "tests_only"
    """Only the project's tests changed. Suspicious: tests are not the bug."""
    MEANINGFUL = "meaningful"
    """Implementation code was changed in a way that could fix something."""

    @property
    def is_repair(self) -> bool:
        return self is PatchQuality.MEANINGFUL


#: Added lines that only observe behaviour. A diff of nothing but these is
#: instrumentation, not a fix.
_DEBUG_LINE = re.compile(
    r"^\+\s*(print\s*\(|pprint\s*\(|logging\.(debug|info|warning)\s*\(|"
    r"log\.(debug|info)\s*\(|breakpoint\s*\(|import pdb|pdb\.set_trace|"
    r"sys\.stderr\.write\s*\(|warnings\.warn\s*\()",
    re.IGNORECASE,
)

_FILE_HEADER = re.compile(r"^\+\+\+ b/(.+)$")


@dataclass(frozen=True)
class PatchAssessment:
    """The verdict on a diff, with the detail needed to explain it."""

    quality: PatchQuality
    source_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    scratch_files: tuple[str, ...] = ()
    added_lines: int = 0
    removed_lines: int = 0
    debug_lines: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def changed_files(self) -> tuple[str, ...]:
        """Every path this diff touches, in a stable order.

        Read from the diff itself, which is what makes it safe to show a
        reviewer: the run's own list of files it edited includes scratch work
        that never reaches the submitted patch.
        """
        return (*self.source_files, *self.test_files, *self.scratch_files)

    @property
    def is_repair(self) -> bool:
        return self.quality.is_repair

    def describe(self) -> str:
        """One line the agent and the event log can both use."""
        if self.quality is PatchQuality.NO_PATCH:
            return "no changes in the working tree"
        if self.quality is PatchQuality.SCRATCH_ONLY:
            return (
                f"only scratch files changed ({', '.join(self.scratch_files)}); "
                "these are excluded from the submitted patch"
            )
        if self.quality is PatchQuality.DEBUG_ONLY:
            return (
                f"{', '.join(self.source_files)} changed, but every added line is "
                "a print or logging call -- that is instrumentation, not a fix"
            )
        if self.quality is PatchQuality.TESTS_ONLY:
            return f"only tests changed ({', '.join(self.test_files)}); the bug is in the code"
        return f"implementation changed in {', '.join(self.source_files)}"

    def to_dict(self) -> dict[str, object]:
        return {
            "quality": str(self.quality),
            "is_repair": self.is_repair,
            "source_files": list(self.source_files),
            "test_files": list(self.test_files),
            "scratch_files": list(self.scratch_files),
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "debug_lines": self.debug_lines,
        }


def assess_patch(diff: str) -> PatchAssessment:
    """Classify a unified diff as a repair or as investigation debris."""
    if not diff or not diff.strip():
        return PatchAssessment(PatchQuality.NO_PATCH)

    source: list[str] = []
    tests: list[str] = []
    scratch: list[str] = []
    added = removed = debug = 0
    source_added = source_debug = 0
    current: WriteKind | None = None

    for line in diff.splitlines():
        header = _FILE_HEADER.match(line)
        if header:
            path = header.group(1).strip()
            current = classify_write(path)
            if current is WriteKind.SOURCE:
                source.append(path)
            elif current is WriteKind.TEST:
                tests.append(path)
            else:
                scratch.append(path)
            continue
        if line.startswith("-") and not line.startswith("---"):
            removed += 1
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added += 1
        is_debug = bool(_DEBUG_LINE.match(line))
        if is_debug:
            debug += 1
        if current is WriteKind.SOURCE:
            source_added += 1
            if is_debug or not line[1:].strip():
                source_debug += 1

    def verdict(quality: PatchQuality) -> PatchAssessment:
        return PatchAssessment(
            quality=quality,
            source_files=tuple(source),
            test_files=tuple(tests),
            scratch_files=tuple(scratch),
            added_lines=added,
            removed_lines=removed,
            debug_lines=debug,
        )

    if not source and not tests:
        # Only scratch paths, or a diff that deletes things from scratch files.
        return verdict(PatchQuality.SCRATCH_ONLY)
    if not source:
        return verdict(PatchQuality.TESTS_ONLY)
    if source_added and source_added == source_debug:
        # Every line this diff adds to implementation code merely observes it.
        # Deletions alone are still a real change, hence the source_added guard.
        return verdict(PatchQuality.DEBUG_ONLY)
    return verdict(PatchQuality.MEANINGFUL)
