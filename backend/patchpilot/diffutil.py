"""Unified-diff parsing.

Rendering was the original need: parsing happens server-side so the frontend
gets a plain data structure instead of shipping a diff parser to the browser,
and each line carries its old and new line numbers, which is what a two-column
diff view wants.

:func:`changed_paths` serves a different question -- *which files does this diff
touch* -- asked by publication, when it checks the commit it built against the
patch that was approved, and by the workspace, when it refuses a patch that
would write somewhere it must not. Both read the same parser here, because two
implementations drifting apart would mean the check guarding a push and the
check guarding a write disagreeing about what a diff does.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DEV_NULL = "/dev/null"
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


def changed_paths(diff: str) -> frozenset[str]:
    """Every repository path a unified diff touches.

    Both sides are reported, so a rename names its source and its destination.
    Two very different parts of PatchPilot ask this question and must not
    disagree: publication compares the file set of the commit it built against
    the set the approved patch declares, and the workspace refuses a patch that
    would edit a protected path before applying it. One parser, one answer.
    """
    paths: set[str] = set()
    for entry in parse_unified_diff(diff):
        for candidate in (entry.path, entry.old_path):
            if candidate and candidate != _DEV_NULL:
                paths.add(candidate)
    return frozenset(paths)


def _strip_prefix(path: str) -> str:
    cleaned = path.strip().split("\t")[0]
    if len(cleaned) >= 2 and cleaned.startswith('"') and cleaned.endswith('"'):
        # git C-quotes any path with unusual bytes in it. Left encoded, the
        # quotes and escapes end up in the rendered filename and in every
        # comparison made against it.
        cleaned = _unquote(cleaned[1:-1])
    for prefix in ("a/", "b/"):
        if cleaned.startswith(prefix):
            return cleaned[2:]
    return cleaned


def _unquote(body: str) -> str:
    """Decode git's C-style quoting for a path."""
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 >= len(body):
            out.append(char)
            index += 1
            continue
        nxt = body[index + 1]
        if nxt in escapes:
            out.append(escapes[nxt])
            index += 2
        elif nxt.isdigit():
            try:
                out.append(chr(int(body[index + 1 : index + 4], 8)))
                index += 4
            except ValueError:
                out.append(char)
                index += 1
        else:
            out.append(nxt)
            index += 2
    return "".join(out)
