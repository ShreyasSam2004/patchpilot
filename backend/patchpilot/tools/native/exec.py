"""Execution tools: run_tests and run_command.

Both go through the sandbox and the command guard. Neither ever invokes a
shell: the model supplies an argv list, or a string that we tokenise with
``shlex`` and then validate. That removes command chaining, redirection and
substitution as an attack surface.
"""

from __future__ import annotations

import shlex
import sys

from pydantic import BaseModel, Field, field_validator

from ...errors import ToolError
from ...sandbox.guard import check_command, sanitize_command
from ..base import Tool, ToolContext, ToolResult
from .testparse import extract_failure_excerpt, parse_test_output

_IS_WINDOWS = sys.platform == "win32"


def _tokenize(command: str | list[str]) -> list[str]:
    """Split a command string into an argv vector.

    POSIX-mode ``shlex`` treats backslash as an escape character, which
    destroys Windows paths: ``C:\\Python\\python.exe`` becomes
    ``C:Pythonpython.exe``. On Windows we parse in non-POSIX mode (backslash is
    literal) and strip the quote characters shlex leaves behind.
    """
    if isinstance(command, list):
        argv = [str(part) for part in command]
    else:
        try:
            argv = shlex.split(command, posix=not _IS_WINDOWS)
        except ValueError as exc:
            raise ToolError(f"could not parse command: {exc}", code="bad_command") from exc
        if _IS_WINDOWS:
            argv = [_unquote(token) for token in argv]
    if not argv:
        raise ToolError("empty command", code="bad_command")
    return argv


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


class RunTestsArgs(BaseModel):
    command: str | list[str] | None = Field(
        default=None,
        description=(
            "Test command to run, e.g. 'python -m pytest tests/test_token.py -x'. "
            "Omit to use the run's configured default test command. Run targeted tests "
            "first, then broaden."
        ),
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Repository-relative directory to run the tests from. Needed for "
            "monorepos where the suite lives in a subdirectory, e.g. 'backend'. "
            "Defaults to the run's discovered test directory, else the repo root."
        ),
    )
    timeout_s: int | None = Field(
        default=None, ge=5, le=3600, description="Override the test timeout."
    )

    @field_validator("command")
    @classmethod
    def _non_empty(cls, value: str | list[str] | None) -> str | list[str] | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class RunTestsTool(Tool[RunTestsArgs]):
    name = "run_tests"
    description = (
        "Run the project's tests inside the sandbox and report pass/fail counts plus the "
        "failure output. This is how you verify a fix. Start with the specific test file "
        "related to the issue, then run the broader suite to check for regressions."
    )
    args_model = RunTestsArgs
    timeout_s = 1800.0

    async def execute(self, ctx: ToolContext, args: RunTestsArgs) -> ToolResult:
        default = ctx.state.get("test_command") or "python -m pytest -q"
        cleaned = sanitize_command(_tokenize(args.command or default))
        argv = cleaned.argv
        cwd = args.cwd if args.cwd is not None else (cleaned.cwd or ctx.state.get("test_cwd"))

        verdict = check_command(argv)
        if not verdict:
            raise ToolError(f"refused: {verdict.reason}", code="command_refused")

        timeout = min(
            args.timeout_s or ctx.settings.max_test_runtime_s,
            ctx.settings.max_test_runtime_s,
        )
        command_str = shlex.join(argv)
        if cwd:
            command_str = f"({cwd}) {command_str}"

        if ctx.emit is not None:
            await ctx.emit.test_started(command_str)

        result = await ctx.sandbox.exec(argv, cwd=cwd, timeout_s=timeout)
        outcome = parse_test_output(
            result.stdout, result.stderr, result.exit_code, timed_out=result.timed_out
        )

        excerpt = ""
        if not outcome.green:
            excerpt = extract_failure_excerpt(
                result.stdout, result.stderr, limit=ctx.settings.max_tool_output_bytes
            )

        ctx.state["last_test_outcome"] = outcome
        ctx.state["last_test_command"] = command_str
        ctx.state.setdefault("test_run_count", 0)
        ctx.state["test_run_count"] += 1

        if ctx.emit is not None:
            await ctx.emit.test_finished(
                command=command_str,
                outcome=outcome,
                duration_ms=result.duration_ms,
                output=excerpt or result.combined_output[:8000],
            )

        header = (
            f"$ {command_str}\n{outcome.describe()} "
            f"(exit={result.exit_code}, {result.duration_ms}ms)"
        )
        if outcome.failing_tests:
            listed = "\n".join(f"  - {name}" for name in outcome.failing_tests[:20])
            header += f"\nFailing tests:\n{listed}"

        body = f"{header}\n\n{excerpt}" if excerpt else header
        return ToolResult(
            ok=True,  # a failing test is a valid observation, not a tool failure
            content=body,
            summary=f"tests: {outcome.describe()}",
            truncated=result.truncated,
            data={
                "command": command_str,
                "passed": outcome.passed,
                "failed": outcome.failed,
                "errors": outcome.errors,
                "skipped": outcome.skipped,
                "total": outcome.total,
                "green": outcome.green,
                "framework": outcome.framework,
                "duration_ms": result.duration_ms,
                "failing_tests": outcome.failing_tests,
                "timed_out": result.timed_out,
            },
        )


class RunCommandArgs(BaseModel):
    command: str | list[str] = Field(
        description=(
            "Command to run as an argv vector or a simple string. No shell is used, so "
            "pipes, redirection, && and $() are refused."
        )
    )
    cwd: str | None = Field(default=None, description="Repository-relative working directory.")
    timeout_s: int | None = Field(default=None, ge=1, le=1800)


class RunCommandTool(Tool[RunCommandArgs]):
    name = "run_command"
    description = (
        "Run a non-test command inside the sandbox — a linter, a type checker, a build step, "
        "or a small reproduction script. Destructive, credential-touching and network commands "
        "are refused."
    )
    args_model = RunCommandArgs
    timeout_s = 900.0

    async def execute(self, ctx: ToolContext, args: RunCommandArgs) -> ToolResult:
        cleaned = sanitize_command(_tokenize(args.command))
        argv = cleaned.argv
        cwd = args.cwd if args.cwd is not None else cleaned.cwd
        verdict = check_command(argv)
        if not verdict:
            raise ToolError(f"refused: {verdict.reason}", code="command_refused")

        timeout = min(
            args.timeout_s or ctx.settings.command_timeout_s,
            ctx.settings.command_timeout_s,
        )
        result = await ctx.sandbox.exec(argv, cwd=cwd, timeout_s=timeout)

        budget = ctx.settings.max_tool_output_bytes
        output = result.combined_output
        truncated = len(output) > budget
        if truncated:
            output = output[:budget] + "\n...[output truncated]"

        status = "TIMED OUT" if result.timed_out else f"exit={result.exit_code}"
        # Say what was rewritten, so the agent learns the calling convention
        # rather than rediscovering it one refusal at a time.
        note = ("\nnote: " + "; ".join(cleaned.notes)) if cleaned.notes else ""
        return ToolResult(
            ok=True,
            content=f"$ {shlex.join(argv)}\n{status} ({result.duration_ms}ms){note}\n\n{output}",
            summary=f"ran {argv[0]} ({status})",
            truncated=truncated or result.truncated,
            data={
                "command": shlex.join(argv),
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "timed_out": result.timed_out,
            },
        )
