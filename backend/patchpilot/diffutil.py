"""Unified-diff parsing for the UI.

Parsing happens server-side so the frontend renders a plain data structure
instead of shipping a diff parser to the browser. Each line carries its old and
new line numbers, which is what a two-column diff view needs.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT = re.compile(r"^diff --git a/(.+?) b/(.+)$")


@dataclass
class DiffLine:
    kind: str  # "context" | "add" | "remove" | "meta"
    content: str
    old_line: int | None = None
    new_line: int | None = None


@dataclass
class DiffHunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class DiffFile:
    path: str
    old_path: str
    additions: int = 0
    deletions: int = 0
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[DiffHunk] = field(default_factory=list)


def parse_unified_diff(diff: str) -> list[DiffFile]:
    """Parse ``git diff`` output into structured files and hunks."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: DiffHunk | None = None
    old_no = new_no = 0

    for line in (diff or "").splitlines():
        header = _DIFF_GIT.match(line)
        if header:
            current = DiffFile(path=header.group(2), old_path=header.group(1))
            files.append(current)
            hunk = None
            continue

        if current is None:
            # Diffs without a `diff --git` preamble (e.g. plain `diff -u`).
            if line.startswith("--- "):
                current = DiffFile(path="", old_path=_strip_prefix(line[4:]))
                files.append(current)
            continue

        if line.startswith("--- "):
            current.old_path = _strip_prefix(line[4:])
            current.is_new = line[4:].strip() == "/dev/null"
            continue
        if line.startswith("+++ "):
            path = _strip_prefix(line[4:])
            current.is_deleted = line[4:].strip() == "/dev/null"
            if not current.is_deleted:
                current.path = path
            continue
        if line.startswith("new file mode"):
            current.is_new = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        if line.startswith(("index ", "similarity index", "rename ", "old mode", "new mode")):
            continue

        match = _HUNK_HEADER.match(line)
        if match:
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_start = int(match.group(3))
            new_count = int(match.group(4) or 1)
            hunk = DiffHunk(
                header=line,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
            )
            current.hunks.append(hunk)
            old_no, new_no = old_start, new_start
            continue

        if hunk is None:
            continue

        if line.startswith("+"):
            hunk.lines.append(DiffLine("add", line[1:], None, new_no))
            new_no += 1
            current.additions += 1
        elif line.startswith("-"):
            hunk.lines.append(DiffLine("remove", line[1:], old_no, None))
            old_no += 1
            current.deletions += 1
        elif line.startswith("\\"):
            hunk.lines.append(DiffLine("meta", line, None, None))
        else:
            content = line[1:] if line.startswith(" ") else line
            hunk.lines.append(DiffLine("context", content, old_no, new_no))
            old_no += 1
            new_no += 1

    return files


def diff_to_dict(diff: str) -> dict[str, Any]:
    """Structured diff payload for the API."""
    files = parse_unified_diff(diff)
    return {
        "raw": diff or "",
        "files": [
            {
                "path": f.path or f.old_path,
                "old_path": f.old_path,
                "additions": f.additions,
                "deletions": f.deletions,
                "is_new": f.is_new,
                "is_deleted": f.is_deleted,
                "hunks": [asdict(h) for h in f.hunks],
            }
            for f in files
        ],
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
    }


def diff_stats(diff: str) -> tuple[int, int, int]:
    """``(files_changed, additions, deletions)`` without full parsing."""
    files = parse_unified_diff(diff)
    return len(files), sum(f.additions for f in files), sum(f.deletions for f in files)


def _strip_prefix(path: str) -> str:
    cleaned = path.strip().split("\t")[0]
    for prefix in ("a/", "b/"):
        if cleaned.startswith(prefix):
            return cleaned[2:]
    return cleaned
