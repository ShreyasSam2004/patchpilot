"""Editing tools: apply_patch and edit_file.

``apply_patch`` is the preferred editor — a unified diff is reviewable, minimal,
and fails loudly when the model's mental model of the file is stale.
``edit_file`` (exact string replacement) exists because diff hunks with wrong
line numbers are the single most common way an LLM edit fails; a unique-anchor
replacement sidesteps that without ever rewriting a whole file.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ...errors import ToolError
from ..base import Tool, ToolContext, ToolResult


class ApplyPatchArgs(BaseModel):
    diff: str = Field(
        min_length=1,
        description=(
            "A unified diff in `git diff` format. Include 'diff --git a/PATH b/PATH', "
            "'--- a/PATH', '+++ b/PATH' headers and @@ hunks with at least 3 lines of "
            "context. Paths are repository-relative."
        ),
    )


def _looks_like_unified_diff(text: str) -> bool:
    return "@@" in text and ("--- " in text or "diff --git" in text)


class ApplyPatchTool(Tool[ApplyPatchArgs]):
    name = "apply_patch"
    description = (
        "Apply a unified diff to the repository. This is the preferred way to change code: "
        "keep the patch minimal and focused on the root cause. If the patch does not apply, "
        "re-read the file to refresh your view of the exact current lines and try again."
    )
    args_model = ApplyPatchArgs
    timeout_s = 120.0
    mutating = True

    async def execute(self, ctx: ToolContext, args: ApplyPatchArgs) -> ToolResult:
        diff = args.diff
        if not _looks_like_unified_diff(diff):
            raise ToolError(
                "that does not look like a unified diff (no @@ hunk header found). "
                "Use `git diff` format, or use edit_file for a simple replacement.",
                code="not_a_diff",
            )

        workspace = ctx.workspace
        before = await workspace.diff_stat()
        result = await workspace.apply_patch(diff)

        if not result.ok:
            stderr = (result.stderr or result.stdout).strip()
            hint = ""
            if "corrupt patch" in stderr or "malformed" in stderr:
                hint = (
                    " The diff is malformed. The @@ header counts must match the hunk body: "
                    "for '@@ -a,b +c,d @@', b is the number of ' ' and '-' lines and d is the "
                    "number of ' ' and '+' lines. Consider edit_file instead."
                )
            elif "does not exist" in stderr or "No such file" in stderr:
                hint = " The target path may be wrong — check it with list_files."
            elif "patch does not apply" in stderr or "while searching for" in stderr:
                hint = (
                    " The context lines do not match the file on disk. Re-read the exact "
                    "region with read_file and rebuild the hunk."
                )
            return ToolResult.failure(
                f"patch did not apply.\n{stderr[:1500]}{hint}",
                code="patch_failed",
            )

        after = await workspace.diff_stat()
        touched = sorted(set(after.files) - set(before.files)) or after.files
        message = (
            f"Patch applied. Working tree now: {after.files_changed} files changed, "
            f"+{after.insertions}/-{after.deletions}."
        )
        if ctx.emit is not None:
            await ctx.emit.patch_applied(
                files=touched,
                insertions=after.insertions,
                deletions=after.deletions,
                diff=diff,
            )
        return ToolResult.success(
            message,
            summary=f"applied patch to {', '.join(touched[:3]) or 'workspace'}",
            files=touched,
            files_changed=after.files_changed,
            insertions=after.insertions,
            deletions=after.deletions,
        )


class EditFileArgs(BaseModel):
    path: str = Field(description="Repository-relative file path.")
    old_string: str = Field(
        min_length=1,
        description=(
            "Exact text to replace, copied verbatim from the file including indentation. "
            "Must occur exactly once unless replace_all is true."
        ),
    )
    new_string: str = Field(description="Replacement text. May be empty to delete.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class EditFileTool(Tool[EditFileArgs]):
    name = "edit_file"
    description = (
        "Replace an exact string in a file. The old_string must match the file byte-for-byte "
        "including indentation, and must be unique unless replace_all is set. Use this when "
        "a diff is awkward; it never rewrites the rest of the file."
    )
    args_model = EditFileArgs
    timeout_s = 30.0
    mutating = True

    async def execute(self, ctx: ToolContext, args: EditFileArgs) -> ToolResult:
        workspace = ctx.workspace
        target = workspace.resolve(args.path, must_exist=True)
        if target.is_dir():
            raise ToolError(f"{args.path} is a directory", code="is_dir")

        original = target.read_text(encoding="utf-8", errors="replace")
        occurrences = original.count(args.old_string)

        if occurrences == 0:
            raise ToolError(
                f"old_string was not found in {args.path}. Re-read the file — indentation and "
                "whitespace must match exactly.",
                code="no_match",
            )
        if occurrences > 1 and not args.replace_all:
            raise ToolError(
                f"old_string occurs {occurrences} times in {args.path}. Include more surrounding "
                "context to make it unique, or set replace_all=true.",
                code="ambiguous_match",
            )

        updated = (
            original.replace(args.old_string, args.new_string)
            if args.replace_all
            else original.replace(args.old_string, args.new_string, 1)
        )
        if updated == original:
            raise ToolError("replacement produced no change", code="noop_edit")

        workspace.write_text(args.path, updated)
        stat = await workspace.diff_stat()
        if ctx.emit is not None:
            await ctx.emit.patch_applied(
                files=[args.path],
                insertions=stat.insertions,
                deletions=stat.deletions,
                diff=await workspace.diff(),
            )
        replaced = occurrences if args.replace_all else 1
        return ToolResult.success(
            f"Replaced {replaced} occurrence(s) in {args.path}. Working tree: "
            f"{stat.files_changed} files changed, +{stat.insertions}/-{stat.deletions}.",
            summary=f"edited {args.path}",
            path=args.path,
            replaced=replaced,
            files=[args.path],
        )
