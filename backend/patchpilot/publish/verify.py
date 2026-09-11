"""What the approved patch claims to touch, read from the patch itself.

Everything else in publication derives the file set from git: `git apply` runs,
then `git diff` says what changed. That is circular if the question is "did the
commit change something the patch did not". Answering it needs a second,
independent reading of the approved bytes -- which is what this module is.

It is deliberately dependency-free and works on strings, so the guard it feeds
can be tested without a repository.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Where GitHub keeps Actions definitions. Pushing a ref whose tree differs from
#: the destination's default branch *here* is what triggers the server-side
#: workflow permission check, so this prefix gets its own error path.
WORKFLOW_DIR = ".github/workflows/"

_DEV_NULL = "/dev/null"


def changed_paths(diff: str) -> frozenset[str]:
    """Every repository path a unified diff claims to add, remove or modify.

    Read from the ``---``/``+++`` header pair, which carries the real path even
    for renames, and falls back to ``diff --git`` for headers that have no hunks
    at all (a pure mode change, or a binary file).
    """
    paths: set[str] = set()
    pending_git_header: tuple[str, str] | None = None
    saw_file_header = False

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if pending_git_header and not saw_file_header:
                paths.update(p for p in pending_git_header if p)
            pending_git_header = _split_git_header(line)
            saw_file_header = False
        elif line.startswith(("--- ", "+++ ")):
            # `/dev/null` on either side means the file was added or removed;
            # _header_path returns None for it and the other side carries the
            # real name.
            path = _header_path(line[4:])
            if path:
                paths.add(path)
                saw_file_header = True

    if pending_git_header and not saw_file_header:
        paths.update(p for p in pending_git_header if p)
    return frozenset(paths)


def workflow_paths(paths: Iterable[str]) -> frozenset[str]:
    """The subset of ``paths`` that are GitHub Actions workflow definitions."""
    return frozenset(p for p in paths if p.startswith(WORKFLOW_DIR))


def _split_git_header(line: str) -> tuple[str, str] | None:
    """Pull both paths out of a ``diff --git a/x b/y`` line.

    Unquoted paths containing spaces are genuinely ambiguous in this format;
    the ``---``/``+++`` pair is preferred everywhere it exists, so this only has
    to cope with the ordinary cases.
    """
    rest = line[len("diff --git ") :].strip()
    if rest.startswith('"'):
        return None
    marker = rest.find(" b/")
    if marker == -1:
        return None
    return _strip_prefix(rest[:marker]), _strip_prefix(rest[marker + 1 :])


def _header_path(raw: str) -> str | None:
    """Normalise one ``---``/``+++`` operand into a repository-relative path."""
    # git appends a tab and a timestamp for some diff styles.
    candidate = raw.split("\t", 1)[0].strip()
    if candidate.startswith('"') and candidate.endswith('"') and len(candidate) >= 2:
        candidate = _unquote(candidate[1:-1])
    if not candidate or candidate == _DEV_NULL:
        return None
    return _strip_prefix(candidate) or None


def _strip_prefix(path: str) -> str:
    """Drop the ``a/``/``b/`` diff prefix when one is present."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _unquote(body: str) -> str:
    """Decode git's C-style quoting for paths with unusual bytes."""
    out: list[str] = []
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
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
        elif nxt.isdigit() and index + 3 < len(body) + 1:
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


__all__ = ["WORKFLOW_DIR", "changed_paths", "workflow_paths"]
