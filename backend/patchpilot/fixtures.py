"""Fixture bug tasks.

Tiny repositories with deliberately introduced bugs. They exist so the whole
autonomous loop — exploration, hypothesis, patch, test, observe, iterate — can
be exercised in seconds and for free, before spending money on SWE-bench.

Each fixture declares which tests are expected to fail before the fix, so a
fixture run can be graded objectively rather than by eyeballing a diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import BACKEND_ROOT

FIXTURES_DIR = BACKEND_ROOT / "fixtures"


@dataclass(slots=True)
class FixtureTask:
    """One fixture bug."""

    id: str
    title: str
    issue: str
    repo_path: Path
    test_command: str = "python -m pytest tests/ -q"
    repository: str = "fixture"
    expected_failing_tests: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    difficulty: str = "easy"
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "issue": self.issue,
            "repository": self.repository,
            "test_command": self.test_command,
            "expected_failing_tests": self.expected_failing_tests,
            "expected_files": self.expected_files,
            "difficulty": self.difficulty,
            "notes": self.notes,
        }


def _load(directory: Path) -> FixtureTask:
    manifest = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    return FixtureTask(
        id=manifest["id"],
        title=manifest["title"],
        issue=manifest["issue"],
        repo_path=directory / "repo",
        test_command=manifest.get("test_command", "python -m pytest tests/ -q"),
        repository=manifest.get("repository", manifest["id"]),
        expected_failing_tests=manifest.get("expected_failing_tests", []),
        expected_files=manifest.get("expected_files", []),
        difficulty=manifest.get("difficulty", "easy"),
        notes=manifest.get("notes", ""),
    )


def list_fixtures(root: Path | None = None) -> list[FixtureTask]:
    """Every fixture task, sorted by id."""
    base = root or FIXTURES_DIR
    if not base.exists():
        return []
    tasks = [
        _load(directory)
        for directory in sorted(base.iterdir())
        if directory.is_dir() and (directory / "task.json").exists()
    ]
    return tasks


def get_fixture(fixture_id: str, root: Path | None = None) -> FixtureTask:
    """Look up one fixture by id."""
    for task in list_fixtures(root):
        if task.id == fixture_id:
            return task
    available = ", ".join(t.id for t in list_fixtures(root)) or "none found"
    raise KeyError(f"unknown fixture {fixture_id!r}; available: {available}")
