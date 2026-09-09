"""Code search tools: search_code and find_symbol.

Implemented in-process rather than by shelling out to grep/ripgrep, so behaviour
is identical on every platform and inside every sandbox, and so results can be
capped precisely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

from ...errors import ToolError
from ..base import Tool, ToolContext, ToolResult

MAX_SCANNED_FILES = 4000
MAX_FILE_SCAN_BYTES = 2_000_000


@dataclass(slots=True)
class Match:
    path: str
    line_no: int
    line: str


def _compile(query: str, *, regex: bool, case_sensitive: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = query if regex else re.escape(query)
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ToolError(f"invalid regular expression: {exc}", code="invalid_regex") from exc


def _scan(
    workspace,
    pattern: re.Pattern[str],
    path: str,
    include: str | None,
    limit: int,
) -> tuple[list[Match], int, bool]:
    matches: list[Match] = []
    scanned = 0
    truncated = False
    suffixes = None
    if include:
        suffixes = {s if s.startswith(".") else f".{s}" for s in
                    (part.strip().lstrip("*") for part in include.split(",")) if s}

    for file_path in workspace.iter_source_files(path, suffixes=suffixes):
        if scanned >= MAX_SCANNED_FILES:
            truncated = True
            break
        try:
            if file_path.stat().st_size > MAX_FILE_SCAN_BYTES:
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        if not pattern.search(text):
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                matches.append(
                    Match(workspace.relpath(file_path), line_no, line.rstrip()[:300])
                )
                if len(matches) >= limit:
                    return matches, scanned, True
    return matches, scanned, truncated


def _render(matches: list[Match]) -> str:
    by_file: dict[str, list[Match]] = {}
    for match in matches:
        by_file.setdefault(match.path, []).append(match)
    blocks = []
    for path, items in by_file.items():
        lines = "\n".join(f"  {m.line_no}: {m.line}" for m in items)
        blocks.append(f"{path} ({len(items)})\n{lines}")
    return "\n\n".join(blocks)


class SearchCodeArgs(BaseModel):
    query: str = Field(min_length=1, description="Literal text, or a regex when regex=true.")
    path: str = Field(default=".", description="Repository-relative directory to search in.")
    regex: bool = Field(default=False, description="Treat query as a Python regular expression.")
    case_sensitive: bool = Field(default=False)
    include: str | None = Field(
        default=None,
        description="Comma-separated file extensions to restrict the search, e.g. '.py,.cfg'.",
    )
    limit: int = Field(default=60, ge=1, le=300, description="Maximum matching lines to return.")


class SearchCodeTool(Tool[SearchCodeArgs]):
    name = "search_code"
    description = (
        "Search the repository for text or a regex, returning file:line matches. This is the "
        "fastest way to localise a bug: search for identifiers from the issue, error message "
        "strings, and exception class names."
    )
    args_model = SearchCodeArgs
    timeout_s = 60.0

    async def execute(self, ctx: ToolContext, args: SearchCodeArgs) -> ToolResult:
        pattern = _compile(args.query, regex=args.regex, case_sensitive=args.case_sensitive)
        matches, scanned, truncated = _scan(
            ctx.workspace, pattern, args.path, args.include, args.limit
        )
        if not matches:
            return ToolResult.success(
                f"No matches for {args.query!r} in {args.path} ({scanned} files scanned). "
                "Try a shorter substring, a different identifier, or regex=true.",
                summary=f"search '{args.query}' — 0 matches",
                query=args.query,
                match_count=0,
                files=[],
            )
        files = sorted({m.path for m in matches})
        body = _render(matches)
        note = "\n[result limit reached; narrow the query or set include=]" if truncated else ""
        return ToolResult(
            ok=True,
            content=(
                f"{len(matches)} matches in {len(files)} files "
                f"for {args.query!r}:\n\n{body}{note}"
            ),
            summary=f"search '{args.query}' — {len(matches)} matches in {len(files)} files",
            truncated=truncated,
            data={
                "query": args.query,
                "match_count": len(matches),
                "files": files,
            },
        )


class FindSymbolArgs(BaseModel):
    symbol: str = Field(min_length=1, description="Function, class, method or variable name.")
    path: str = Field(default=".", description="Repository-relative directory to search in.")
    limit: int = Field(default=40, ge=1, le=200)


#: Definition patterns for the languages SWE-bench Lite actually contains,
#: plus the common ones. ``{s}`` is replaced with the escaped symbol.
DEFINITION_PATTERNS: tuple[str, ...] = (
    r"^\s*(?:async\s+)?def\s+{s}\s*\(",          # python function/method
    r"^\s*class\s+{s}\s*[\(:]",                   # python class
    r"^\s*{s}\s*=\s*",                            # python module-level binding
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+{s}\s*\(",   # js/ts
    r"^\s*(?:export\s+)?(?:default\s+)?class\s+{s}\b",       # js/ts class
    r"^\s*(?:const|let|var)\s+{s}\s*[=:]",        # js/ts binding
    r"^\s*(?:public|private|protected|static|\s)*[\w<>\[\],\s]+\s+{s}\s*\(",  # java/c#
    r"^\s*func\s+(?:\([^)]*\)\s*)?{s}\s*\(",      # go
    r"^\s*(?:pub\s+)?fn\s+{s}\s*[<(]",            # rust
    r"^\s*def\s+{s}\b",                           # ruby/scala
)


class FindSymbolTool(Tool[FindSymbolArgs]):
    name = "find_symbol"
    description = (
        "Find where a function, class or variable is *defined* (not merely mentioned). "
        "Use this after search_code narrows the terminology, to jump straight to the "
        "definition instead of reading whole files."
    )
    args_model = FindSymbolArgs
    timeout_s = 60.0

    async def execute(self, ctx: ToolContext, args: FindSymbolArgs) -> ToolResult:
        # Models routinely pass "def foo" or "class Bar" here. Taking the bare
        # identifier is friendlier than returning nothing.
        symbol = re.sub(r"^\s*(?:async\s+)?(?:def|class|function|fn)\s+", "", args.symbol).strip()
        symbol = symbol.split("(")[0].strip() or args.symbol
        escaped = re.escape(symbol)
        pattern = re.compile(
            "|".join(f"(?:{p.format(s=escaped)})" for p in DEFINITION_PATTERNS),
            re.MULTILINE,
        )
        matches, scanned, truncated = _scan(ctx.workspace, pattern, args.path, None, args.limit)
        if not matches:
            fallback = _compile(symbol, regex=False, case_sensitive=True)
            references, scanned, _ = _scan(ctx.workspace, fallback, args.path, None, 20)
            if references:
                body = _render(references)
                return ToolResult.success(
                    f"No definition of {symbol!r} found, but it is referenced in "
                    f"{len({r.path for r in references})} files:\n\n{body}",
                    summary=(
                        f"find_symbol '{args.symbol}' — no definition, "
                        f"{len(references)} references"
                    ),
                    symbol=args.symbol,
                    definition_count=0,
                    files=sorted({r.path for r in references}),
                )
            return ToolResult.success(
                f"{symbol!r} does not appear in {args.path} ({scanned} files scanned).",
                summary=f"find_symbol '{symbol}' — not found",
                symbol=symbol,
                definition_count=0,
                files=[],
            )
        files = sorted({m.path for m in matches})
        return ToolResult(
            ok=True,
            content=f"Definitions of {symbol!r}:\n\n{_render(matches)}",
            summary=f"find_symbol '{symbol}' — {len(matches)} definitions",
            truncated=truncated,
            data={"symbol": symbol, "definition_count": len(matches), "files": files},
        )
