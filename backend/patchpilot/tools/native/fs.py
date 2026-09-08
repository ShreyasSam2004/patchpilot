"""Filesystem tools: list_files, read_file, write_file."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...errors import ToolError
from ..base import Tool, ToolContext, ToolResult


class ListFilesArgs(BaseModel):
    path: str = Field(
        default=".", description="Repository-relative directory. Defaults to the repo root."
    )
    depth: int = Field(default=1, ge=1, le=4, description="How many directory levels to descend.")
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum entries to return.")


class ListFilesTool(Tool[ListFilesArgs]):
    name = "list_files"
    description = (
        "List files and directories inside the repository. Use this to orient yourself "
        "before reading. Directories end with '/'. Vendor directories (.git, node_modules, "
        ".venv, __pycache__) are skipped."
    )
    args_model = ListFilesArgs
    timeout_s = 30.0

    async def execute(self, ctx: ToolContext, args: ListFilesArgs) -> ToolResult:
        entries = ctx.workspace.list_dir(args.path, depth=args.depth, limit=args.limit)
        if not entries:
            return ToolResult.success(
                f"{args.path} is empty.", summary=f"listed {args.path} (empty)", entries=[]
            )
        body = "\n".join(entries)
        return ToolResult.success(
            f"{len(entries)} entries under {args.path}:\n{body}",
            summary=f"listed {args.path} ({len(entries)} entries)",
            entries=entries,
            path=args.path,
        )


class ReadFileArgs(BaseModel):
    path: str = Field(description="Repository-relative file path.")
    start_line: int | None = Field(
        default=None, ge=1, description="1-indexed first line to return."
    )
    end_line: int | None = Field(default=None, ge=1, description="1-indexed last line, inclusive.")


class ReadFileTool(Tool[ReadFileArgs]):
    name = "read_file"
    description = (
        "Read a text file from the repository with line numbers. Pass start_line/end_line "
        "to read only a region — prefer that for large files rather than reading everything."
    )
    args_model = ReadFileArgs
    timeout_s = 30.0

    async def execute(self, ctx: ToolContext, args: ReadFileArgs) -> ToolResult:
        content, truncated = ctx.workspace.read_text(args.path, ctx.settings.max_file_read_bytes)
        lines = content.splitlines()
        total = len(lines)

        start = args.start_line or 1
        end = args.end_line or total
        if args.start_line and args.end_line and args.end_line < args.start_line:
            raise ToolError("end_line must be >= start_line", code="invalid_range")
        start = max(1, min(start, total or 1))
        end = max(start, min(end, total))

        selected = lines[start - 1 : end]
        width = len(str(end))
        numbered = "\n".join(
            f"{str(start + i).rjust(width)}| {line}" for i, line in enumerate(selected)
        )

        header = f"{args.path} (lines {start}-{end} of {total})"
        notes = []
        if truncated:
            notes.append("file exceeded the read budget and was cut short")
        if end < total:
            notes.append(f"{total - end} more lines below")
        suffix = f"\n[{'; '.join(notes)}]" if notes else ""

        return ToolResult(
            ok=True,
            content=f"{header}\n{numbered}{suffix}",
            summary=f"read {args.path} ({end - start + 1} lines)",
            truncated=truncated,
            data={
                "path": args.path,
                "start_line": start,
                "end_line": end,
                "total_lines": total,
            },
        )


class WriteFileArgs(BaseModel):
    path: str = Field(description="Repository-relative file path. Parent directories are created.")
    content: str = Field(description="Complete new file content.")


class WriteFileTool(Tool[WriteFileArgs]):
    name = "write_file"
    description = (
        "Replace a file's entire contents. Prefer apply_patch for edits to existing files — "
        "whole-file rewrites lose surrounding context and cause accidental regressions. "
        "Use this for genuinely new files."
    )
    args_model = WriteFileArgs
    timeout_s = 30.0
    mutating = True

    async def execute(self, ctx: ToolContext, args: WriteFileArgs) -> ToolResult:
        workspace = ctx.workspace
        target = workspace.resolve(args.path)
        existed = target.exists()
        previous_lines = 0
        if existed:
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
                previous_lines = len(text.splitlines())
            except OSError:
                previous_lines = 0

        workspace.write_text(args.path, args.content)
        new_lines = len(args.content.splitlines())
        verb = "updated" if existed else "created"
        return ToolResult.success(
            f"{verb} {args.path} ({new_lines} lines, was {previous_lines})",
            summary=f"{verb} {args.path}",
            path=args.path,
            created=not existed,
            lines=new_lines,
        )
