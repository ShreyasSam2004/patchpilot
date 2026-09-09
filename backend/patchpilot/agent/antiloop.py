"""Repetition and stall detection.

The characteristic failure of an autonomous coding agent is not a crash — it is
a confident loop: re-reading the same file, re-running the same failing test
without changing anything, or re-applying a patch it already tried.

This module watches four signals:

1. **Identical action repeats** — the same tool with canonically identical
   arguments, seen too many times.
2. **Identical patch repeats** — a patch whose normalised content was already
   applied (or already rejected).
3. **Test churn** — running tests repeatedly with no intervening mutation.
4. **No progress** — many iterations with no mutating action at all.

When a signal fires, the orchestrator injects a recovery directive rather than
killing the run: the agent is told what it has already established, which
assumption is suspect, and what to try instead.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: Arguments that do not change what an action *means* and so should not make
#: two otherwise identical calls look different.
_VOLATILE_KEYS = frozenset({"timeout_s", "limit"})


def canonical_action(tool: str, arguments: dict[str, Any]) -> str:
    """A stable fingerprint for "the agent did this exact thing"."""
    filtered = {k: v for k, v in sorted((arguments or {}).items()) if k not in _VOLATILE_KEYS}
    blob = json.dumps(filtered, sort_keys=True, default=str, ensure_ascii=False)
    return f"{tool}:{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}"


def normalize_patch(diff: str) -> str:
    """Hash a patch ignoring whitespace, line numbers and index headers.

    Two diffs that produce the same edit differ constantly in ``@@`` offsets and
    ``index abc..def`` lines; without normalising, near-identical retries look
    like progress.
    """
    lines: list[str] = []
    for line in diff.splitlines():
        if line.startswith(("index ", "@@")) or not line.strip():
            continue
        lines.append(re.sub(r"\s+", " ", line).strip())
    blob = "\n".join(lines)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class LoopSignal:
    """A detected repetition, with the recovery directive to inject."""

    kind: str
    detail: str
    directive: str
    severity: int = 1


@dataclass
class LoopDetector:
    """Tracks the run's action history and reports repetition."""

    action_repeat_threshold: int = 3
    """How many identical actions before it counts as a loop."""
    stall_iterations: int = 4
    """Iterations with no mutating action before we call it a stall."""

    actions: Counter[str] = field(default_factory=Counter)
    action_labels: dict[str, str] = field(default_factory=dict)
    patch_hashes: Counter[str] = field(default_factory=Counter)
    failed_patch_hashes: set[str] = field(default_factory=set)
    tests_since_mutation: int = 0
    iterations_since_mutation: int = 0
    fired: list[str] = field(default_factory=list)
    """Signal kinds already reported, so we escalate instead of repeating."""

    # ------------------------------------------------------------- recording
    def record_action(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        mutating: bool,
        succeeded: bool = True,
    ) -> None:
        key = canonical_action(tool, arguments)
        self.actions[key] += 1
        self.action_labels[key] = _label(tool, arguments)
        if mutating and succeeded:
            self.tests_since_mutation = 0
            self.iterations_since_mutation = 0
        elif tool == "run_tests" and succeeded:
            # Only a test run that actually executed counts as churn. A refused
            # or crashed call did not observe anything, so telling the agent it
            # "ran tests 3 times" would be false and unactionable.
            self.tests_since_mutation += 1

    def record_patch(self, diff: str, *, applied: bool) -> str:
        digest = normalize_patch(diff)
        self.patch_hashes[digest] += 1
        if not applied:
            self.failed_patch_hashes.add(digest)
        return digest

    def end_iteration(self, *, mutated: bool) -> None:
        self.iterations_since_mutation = 0 if mutated else self.iterations_since_mutation + 1

    # ------------------------------------------------------------- detection
    def check(self) -> LoopSignal | None:
        """The most urgent unreported signal, if any."""
        for signal in self._signals():
            if signal.kind not in self.fired:
                self.fired.append(signal.kind)
                return signal
        return None

    def _signals(self) -> list[LoopSignal]:
        found: list[LoopSignal] = []

        for key, count in self.actions.most_common(3):
            if count >= self.action_repeat_threshold:
                label = self.action_labels.get(key, key)
                found.append(
                    LoopSignal(
                        kind=f"repeated_action:{key}",
                        detail=f"{label} has been repeated {count} times with identical arguments",
                        directive=(
                            f"You have now run `{label}` {count} times with the same arguments and "
                            "learned nothing new from the last two. Stop repeating it. State in "
                            "one "
                            "line what that action already established, then take a *different* "
                            "action: widen the search to the whole repository, inspect the tests "
                            "that exercise this code, or look at the callers of the function you "
                            "believe is broken."
                        ),
                        severity=2,
                    )
                )

        for digest, count in self.patch_hashes.most_common(2):
            if count >= 2:
                already_failed = digest in self.failed_patch_hashes
                found.append(
                    LoopSignal(
                        kind=f"repeated_patch:{digest}",
                        detail=f"an equivalent patch has been attempted {count} times",
                        directive=(
                            "You have submitted an effectively identical patch more than once"
                            + (" and it failed to apply both times. " if already_failed else ". ")
                            + "Repeating it will not change the result. Your hypothesis is "
                            "probably "
                            "wrong, not your patch syntax. Re-read the failing test to see what "
                            "behaviour it actually asserts, then record a NEW hypothesis with "
                            "record_hypothesis(supersedes_previous=true) before editing again."
                        ),
                        severity=3,
                    )
                )

        if self.tests_since_mutation >= 3:
            found.append(
                LoopSignal(
                    kind="test_churn",
                    detail=f"{self.tests_since_mutation} test runs without changing any code",
                    directive=(
                        f"You have run tests {self.tests_since_mutation} times without changing "
                        "any "
                        "code in between, so the result cannot change. Read the failure output "
                        "carefully and edit the source, or finish with resolved=false if you "
                        "believe the failure is environmental."
                    ),
                    severity=2,
                )
            )

        if self.iterations_since_mutation >= self.stall_iterations:
            found.append(
                LoopSignal(
                    kind="stalled",
                    detail=f"{self.iterations_since_mutation} iterations with no code change",
                    directive=(
                        f"{self.iterations_since_mutation} iterations have passed without a single "
                        "edit to the repository. Investigation is not the goal — a patch is. "
                        "Summarise what you know, commit to the most plausible root cause, and "
                        "make a concrete minimal edit now. You can always revise it after seeing "
                        "the test results."
                    ),
                    severity=3,
                )
            )

        found.sort(key=lambda s: -s.severity)
        return found

    def snapshot(self) -> dict[str, Any]:
        return {
            "distinct_actions": len(self.actions),
            "max_action_repeat": max(self.actions.values(), default=0),
            "distinct_patches": len(self.patch_hashes),
            "signals_fired": list(self.fired),
            "iterations_since_mutation": self.iterations_since_mutation,
        }


def _label(tool: str, arguments: dict[str, Any]) -> str:
    """Short human-readable form of an action, for messages and the UI."""
    args = arguments or {}
    for key in ("path", "query", "symbol", "command"):
        if args.get(key):
            value = args[key]
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            return f"{tool}({str(value)[:60]})"
    return f"{tool}()"
