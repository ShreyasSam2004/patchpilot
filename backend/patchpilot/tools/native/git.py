"""Read-only git tools: git_diff, git_status, git_log.

Only inspection is exposed. ``git push``/``commit``/``remote`` are absent from
the registry entirely and additionally denied by the default policy, so the
agent has no path to publishing anything.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..base import EmptyArgs, Tool, ToolContext, ToolResult


class GitDiffArgs(BaseModel):
    path: str | None = Field(default=None, description="Limit the diff to one path.")


class GitDiffTool(Tool[GitDiffArgs]):
    name = "git_diff"
    description = (
        "Show the unified diff of every change made so far in this run. Use it to review "
        "your own work before running tests and before finishing."
    )
    args_model = GitDiffArgs
    timeout_s = 60.0

    async def execute(self, ctx: ToolContext, args: GitDiffArgs) -> ToolResult:
        workspace = ctx.workspace
        diff = await workspace.diff()
        if args.path:
            workspace.resolve(args.path)  # validate the path is inside the jail
            kept: list[str] = []
            include = False
            for line in diff.splitlines():
                if line.startswith("diff --git "):
                    include = args.path in line
                if include:
                    kept.append(line)
            diff = "\n".join(kept)

        if not diff.strip():
            return ToolResult.success(
                "No changes in the working tree yet.", summary="git diff — no changes", diff=""
            )
        stat = await workspace.diff_stat()
        budget = ctx.settings.max_tool_output_bytes
        shown = diff if len(diff) <= budget else diff[:budget] + "\n...[diff truncated]"
        return ToolResult(
            ok=True,
            content=shown,
            summary=(
                f"git diff — {stat.files_changed} files, +{stat.insertions}/-{stat.deletions}"
            ),
            truncated=len(diff) > budget,
            data={
                "files": stat.files,
                "insertions": stat.insertions,
                "deletions": stat.deletions,
            },
        )


class GitStatusTool(Tool[EmptyArgs]):
    name = "git_status"
    description = "Show which files are modified, added or untracked in the working tree."
    args_model = EmptyArgs
    timeout_s = 30.0

    async def execute(self, ctx: ToolContext, args: EmptyArgs) -> ToolResult:
        result = await ctx.workspace.git("status", "--porcelain=v1", "--untracked-files=normal")
        if not result.ok:
            return ToolResult.failure(result.stderr.strip()[:500] or "git status failed")
        body = result.stdout.strip()
        if not body:
            return ToolResult.success("Working tree is clean.", summary="git status — clean")
        entries = body.splitlines()
        return ToolResult.success(
            f"{len(entries)} changed paths:\n{body}",
            summary=f"git status — {len(entries)} changed paths",
            entries=entries,
        )


class GitLogArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=50, description="Number of commits to show.")
    path: str | None = Field(default=None, description="Only commits touching this path.")


class GitLogTool(Tool[GitLogArgs]):
    name = "git_log"
    description = (
        "Show recent commit history, optionally for one path. Useful for understanding why "
        "code is written the way it is, and for finding the change that introduced a bug."
    )
    args_model = GitLogArgs
    timeout_s = 60.0

    async def execute(self, ctx: ToolContext, args: GitLogArgs) -> ToolResult:
        argv = ["log", f"-n{args.limit}", "--no-color", "--date=short",
                "--pretty=format:%h %ad %an: %s"]
        if args.path:
            ctx.workspace.resolve(args.path)
            argv += ["--", args.path]
        result = await ctx.workspace.git(*argv)
        if not result.ok:
            return ToolResult.failure(result.stderr.strip()[:500] or "git log failed")
        body = result.stdout.strip() or "no commits"
        return ToolResult.success(
            body, summary=f"git log — {len(body.splitlines())} commits", commits=body.splitlines()
        )
