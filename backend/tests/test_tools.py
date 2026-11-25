"""Tool contract, policy and behaviour tests."""

from __future__ import annotations

import sys

import pytest

from patchpilot.domain.enums import ToolOrigin
from patchpilot.tools.base import Tool, ToolContext, ToolResult
from patchpilot.tools.native import build_native_tools
from patchpilot.tools.native.testparse import extract_failure_excerpt, parse_test_output
from patchpilot.tools.policy import ToolPolicy
from patchpilot.tools.registry import ToolRegistry

# --------------------------------------------------------------------- schema


def test_every_tool_declares_a_complete_contract() -> None:
    for tool in build_native_tools():
        assert tool.name and tool.name.islower()
        assert len(tool.description) > 40, f"{tool.name} needs a usable description"
        schema = tool.json_schema()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert tool.timeout_s > 0


def test_tool_names_are_unique() -> None:
    names = [tool.name for tool in build_native_tools()]
    assert len(names) == len(set(names))


async def test_invalid_arguments_become_an_observation_not_a_crash(tool_ctx: ToolContext) -> None:
    read = next(t for t in build_native_tools() if t.name == "read_file")
    result = await read.run(tool_ctx, {})  # missing required 'path'
    assert not result.ok
    assert result.error_code == "invalid_arguments"
    assert "path" in result.content


async def test_a_crashing_tool_is_contained(tool_ctx: ToolContext) -> None:
    class Boom(Tool):
        name = "boom"
        description = "x" * 50
        args_model = build_native_tools()[0].args_model

        async def execute(self, ctx, args):
            raise RuntimeError("kaboom")

    result = await Boom().run(tool_ctx, {})
    assert not result.ok
    assert result.error_code == "tool_exception"
    assert "kaboom" in result.content


# --------------------------------------------------------------------- policy


def test_policy_is_default_deny() -> None:
    policy = ToolPolicy(allow=["read_file"], deny=[])
    assert policy.permits("read_file")
    assert not policy.permits("run_command")


def test_deny_beats_allow() -> None:
    policy = ToolPolicy(allow=["*"], deny=["run_command"], default_allow=True)
    assert not policy.permits("run_command")
    assert policy.permits("read_file")


def test_policy_supports_patterns() -> None:
    policy = ToolPolicy(allow=["git_*"], deny=["git_push"])
    assert policy.permits("git_diff")
    assert not policy.permits("git_push")
    assert not policy.permits("run_tests")


def test_default_policy_permits_every_native_tool(settings) -> None:
    """The shipped allow-list must cover every tool we actually build.

    Without this, adding a tool and forgetting the allow-list entry silently
    makes it unreachable — the agent sees it in no schema and can never call it.
    """
    policy = ToolPolicy.from_settings(settings)
    missing = [tool.name for tool in build_native_tools() if not policy.permits(tool.name)]
    assert not missing, f"native tools denied by the default policy: {missing}"


def test_default_settings_policy_denies_dangerous_tools(settings) -> None:
    policy = ToolPolicy.from_settings(settings)
    for name in ("git_push", "git_commit", "host_shell", "credential_access"):
        assert not policy.permits(name), name
    for name in ("read_file", "search_code", "run_tests", "apply_patch"):
        assert policy.permits(name), name


# ------------------------------------------------------------------- registry


async def test_registry_refuses_a_denied_tool(tool_ctx: ToolContext) -> None:
    registry = ToolRegistry(policy=ToolPolicy(allow=["read_file"]))
    registry.register_all(build_native_tools())
    result = await registry.call("run_command", tool_ctx, {"command": "echo hi"})
    assert not result.ok
    assert result.error_code == "tool_denied"


async def test_registry_reports_unknown_tools_helpfully(tool_ctx: ToolContext) -> None:
    registry = ToolRegistry(policy=ToolPolicy.permissive())
    registry.register_all(build_native_tools())
    result = await registry.call("teleport", tool_ctx, {})
    assert not result.ok
    assert result.error_code == "tool_not_found"
    assert "read_file" in result.content  # lists what is available


def test_registry_only_exposes_allowed_specs() -> None:
    registry = ToolRegistry(policy=ToolPolicy(allow=["read_file", "search_code"]))
    registry.register_all(build_native_tools())
    names = {spec.name for spec in registry.specs()}
    assert names == {"read_file", "search_code"}
    assert "run_command" in registry.blocked_names


def test_native_tool_wins_a_name_collision() -> None:
    class FakeMcpRead(Tool):
        name = "read_file"
        description = "x" * 50
        origin = ToolOrigin.MCP
        args_model = build_native_tools()[0].args_model

        async def execute(self, ctx, args):
            return ToolResult.success("mcp")

    registry = ToolRegistry(policy=ToolPolicy.permissive())
    registry.register_all(build_native_tools())
    resolved = registry.register(FakeMcpRead(), server="demo")

    assert resolved == "mcp__demo__read_file"
    assert registry.get("read_file").origin is ToolOrigin.NATIVE


async def test_an_mcp_call_that_never_answers_times_out(tool_ctx: ToolContext) -> None:
    """Nothing else bounds a remote call, so the tool must.

    Without this a hung server held the run forever: the budget is only checked
    between iterations, and the heartbeat kept the run looking alive.
    """
    import asyncio

    from patchpilot.tools.mcp.client import McpTool

    class SilentSession:
        async def call_tool(self, name, arguments):
            await asyncio.sleep(3600)

    tool = McpTool(
        session=SilentSession(), server="demo", name="lookup", description="", input_schema={}
    )
    tool.timeout_s = 0.05
    result = await tool.run(tool_ctx, {})
    assert not result.ok
    assert result.error_code == "mcp_timeout"
    assert "did not answer" in result.content


def test_reapplying_a_policy_updates_every_tool() -> None:
    registry = ToolRegistry(policy=ToolPolicy.permissive())
    registry.register_all(build_native_tools())
    assert registry.has("run_command")
    registry.reapply_policy(ToolPolicy(allow=["read_file"]))
    assert not registry.has("run_command")
    assert registry.has("read_file")


# ------------------------------------------------------------ tool behaviour


async def test_list_files(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call("list_files", tool_ctx, {"path": ".", "depth": 2})
    assert result.ok
    assert "src/calc.py" in result.data["entries"]


async def test_read_file_numbers_lines(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call("read_file", tool_ctx, {"path": "src/calc.py"})
    assert result.ok
    assert "1| def add(a, b):" in result.content
    assert result.data["total_lines"] == 6


async def test_read_file_supports_a_line_range(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call(
        "read_file", tool_ctx, {"path": "src/calc.py", "start_line": 5, "end_line": 6}
    )
    assert result.ok
    assert "def divide" in result.content
    assert "def add" not in result.content


async def test_read_file_outside_the_repo_is_refused(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call("read_file", tool_ctx, {"path": "../../../etc/passwd"})
    assert not result.ok
    assert result.error_code == "path_escape"


async def test_search_code_finds_matches(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call("search_code", tool_ctx, {"query": "divide"})
    assert result.ok
    assert result.data["match_count"] >= 1
    assert "src/calc.py" in result.data["files"]


async def test_search_code_reports_no_matches_usefully(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call("search_code", tool_ctx, {"query": "zzz_nonexistent"})
    assert result.ok
    assert result.data["match_count"] == 0
    assert "No matches" in result.content


async def test_search_code_rejects_a_bad_regex(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call("search_code", tool_ctx, {"query": "[unclosed", "regex": True})
    assert not result.ok
    assert result.error_code == "invalid_regex"


async def test_find_symbol_locates_a_definition(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call("find_symbol", tool_ctx, {"symbol": "divide"})
    assert result.ok
    assert result.data["definition_count"] >= 1


async def test_edit_file_replaces_exact_text(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call(
        "edit_file",
        tool_ctx,
        {
            "path": "src/calc.py",
            "old_string": "    return a / b",
            "new_string": "    if b == 0:\n        return None\n    return a / b",
        },
    )
    assert result.ok, result.content
    content = tool_ctx.workspace.resolve("src/calc.py").read_text(encoding="utf-8")
    assert "return None" in content


async def test_edit_file_keeps_a_crlf_files_line_endings(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    """A one-line edit to a CRLF file must not rewrite every line ending.

    The model sees lines without terminators, so its strings use `\\n`; the
    file's own convention has to survive the round trip, or the diff (and the
    review, and the pull request) shows the whole file as changed.
    """
    workspace = tool_ctx.workspace
    await workspace.git("config", "core.autocrlf", "false")  # host config must not mask it
    path = workspace.root / "win.py"
    path.write_bytes(b"def f(x):\r\n    if x:\r\n        return 1\r\n    return 0\r\n")
    await workspace.git("add", "win.py")
    await workspace.git("commit", "--quiet", "-m", "crlf file")

    result = await registry.call(
        "edit_file",
        tool_ctx,
        {
            "path": "win.py",
            "old_string": "    if x:\n        return 1",
            "new_string": "    if x:\n        return 2",
        },
    )
    assert result.ok, result.content
    assert path.read_bytes() == b"def f(x):\r\n    if x:\r\n        return 2\r\n    return 0\r\n"
    # Scoped to this file: the shared fixture's other files were committed
    # under the host's own autocrlf setting, which this test just overrode.
    numstat = await workspace.git("diff", "HEAD", "--numstat", "--", "win.py")
    assert numstat.stdout.split()[:2] == ["1", "1"], numstat.stdout


async def test_edit_file_refuses_to_rewrite_a_non_utf8_file(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    """Decoding with replacement and writing back corrupts every other byte."""
    path = tool_ctx.workspace.root / "legacy.py"
    original = "# caf\xe9\nvalue = 1\n".encode("latin-1")
    path.write_bytes(original)
    result = await registry.call(
        "edit_file",
        tool_ctx,
        {"path": "legacy.py", "old_string": "value = 1", "new_string": "value = 2"},
    )
    assert not result.ok
    assert result.error_code == "not_utf8"
    assert path.read_bytes() == original


async def test_edit_file_refuses_an_ambiguous_anchor(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    tool_ctx.workspace.write_text("dup.py", "x = 1\nx = 1\n")
    result = await registry.call(
        "edit_file", tool_ctx, {"path": "dup.py", "old_string": "x = 1", "new_string": "x = 2"}
    )
    assert not result.ok
    assert result.error_code == "ambiguous_match"


async def test_edit_file_reports_a_missing_anchor(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call(
        "edit_file", tool_ctx, {"path": "src/calc.py", "old_string": "nope", "new_string": "x"}
    )
    assert not result.ok
    assert result.error_code == "no_match"


async def test_apply_patch_rejects_non_diff_input(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call("apply_patch", tool_ctx, {"diff": "just some text"})
    assert not result.ok
    assert result.error_code == "not_a_diff"


async def test_apply_patch_applies_a_real_diff(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -4,3 +4,5 @@ def add(a, b):\n"
        " \n"
        " def divide(a, b):\n"
        "+    if b == 0:\n"
        "+        return None\n"
        "     return a / b\n"
    )
    result = await registry.call("apply_patch", tool_ctx, {"diff": diff})
    assert result.ok, result.content
    assert "return None" in tool_ctx.workspace.resolve("src/calc.py").read_text(encoding="utf-8")


async def test_apply_patch_explains_a_stale_hunk(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    """A well-formed diff whose context no longer matches the file."""
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        # 3 old lines (context, removal, context) and 3 new — well-formed,
        # but the removed line is not what the file actually contains.
        "@@ -1,3 +1,3 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b + 1\n"
        " \n"
    )
    result = await registry.call("apply_patch", tool_ctx, {"diff": diff})
    assert not result.ok
    assert result.error_code == "patch_failed"
    assert "re-read" in result.content.lower() or "context lines" in result.content.lower()


async def test_apply_patch_explains_a_malformed_hunk_header(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    """@@ counts that disagree with the body — a very common LLM diff error."""
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-nonexistent line\n"
        "+replacement\n"
    )
    result = await registry.call("apply_patch", tool_ctx, {"diff": diff})
    assert not result.ok
    assert result.error_code == "patch_failed"
    assert "@@ header counts" in result.content


async def test_git_diff_and_status(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    empty = await registry.call("git_diff", tool_ctx, {})
    assert "No changes" in empty.content

    tool_ctx.workspace.write_text("src/calc.py", "def add(a, b):\n    return a + b + 1\n")
    diff = await registry.call("git_diff", tool_ctx, {})
    assert diff.ok
    assert "src/calc.py" in diff.content

    status = await registry.call("git_status", tool_ctx, {})
    assert status.ok
    assert "calc.py" in status.content


async def test_git_log(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call("git_log", tool_ctx, {"limit": 5})
    assert result.ok
    assert "initial" in result.content


async def test_run_command_refuses_dangerous_input(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call("run_command", tool_ctx, {"command": "sudo rm -rf /"})
    assert not result.ok
    assert result.error_code == "command_refused"


async def test_run_command_executes(registry: ToolRegistry, tool_ctx: ToolContext) -> None:
    result = await registry.call(
        "run_command", tool_ctx, {"command": [sys.executable, "-c", "print('ok')"]}
    )
    assert result.ok
    assert "ok" in result.content
    assert result.data["exit_code"] == 0


async def test_finish_refuses_to_claim_success_without_a_patch(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call(
        "finish", tool_ctx, {"resolved": True, "summary": "all good"}
    )
    assert not result.ok
    assert result.error_code == "no_patch"


async def test_finish_refuses_to_claim_success_without_tests(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    tool_ctx.workspace.write_text("src/calc.py", "def add(a, b):\n    return a + b\n")
    result = await registry.call("finish", tool_ctx, {"resolved": True, "summary": "changed it"})
    assert not result.ok
    assert result.error_code == "untested"


async def test_finish_allows_an_honest_failure(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call(
        "finish", tool_ctx, {"resolved": False, "summary": "could not reproduce"}
    )
    assert result.ok
    assert tool_ctx.state["finish_requested"] is True
    assert tool_ctx.state["finish_resolved"] is False


async def test_record_hypothesis_captures_structure(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call(
        "record_hypothesis",
        tool_ctx,
        {
            "suspected_cause": "divide does not guard against b == 0",
            "evidence": "src/calc.py:6 divides without a check",
            "files_involved": ["src/calc.py"],
            "proposed_fix": "return None when b == 0",
            "validation_plan": "python -m pytest tests/",
        },
    )
    assert result.ok
    assert tool_ctx.state["hypothesis_count"] == 1
    assert tool_ctx.state["current_hypothesis"]["suspected_cause"].startswith("divide")


async def test_record_hypothesis_rejects_an_escaping_path(
    registry: ToolRegistry, tool_ctx: ToolContext
) -> None:
    result = await registry.call(
        "record_hypothesis",
        tool_ctx,
        {
            "suspected_cause": "x" * 10,
            "evidence": "y" * 10,
            "files_involved": ["../../etc/passwd"],
            "proposed_fix": "z" * 10,
            "validation_plan": "pytest",
        },
    )
    assert not result.ok
    assert result.error_code == "path_escape"


# -------------------------------------------------------------- test parsing


def test_parses_a_pytest_failure_summary() -> None:
    output = (
        "collected 185 items\n\n"
        "tests/test_token.py ..F..\n\n"
        "=================================== FAILURES ===================================\n"
        "___ test_expired ___\n"
        "E   assert 500 == 401\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_token.py::test_expired - assert 500 == 401\n"
        "======================= 2 failed, 183 passed in 4.21s ========================\n"
    )
    outcome = parse_test_output(output, "", 1)
    assert outcome.parsed
    assert outcome.framework == "pytest"
    assert outcome.passed == 183
    assert outcome.failed == 2
    assert not outcome.green
    assert "tests/test_token.py::test_expired" in outcome.failing_tests
    assert outcome.duration_s == pytest.approx(4.21)


def test_parses_quiet_mode_output() -> None:
    """`pytest -q` prints no '====' banner around its summary line.

    This is the shape the agent produces by default, so it is the shape that
    matters most.
    """
    output = (
        "....FFFF                                                       [100%]\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_pagination.py::test_total_pages_with_remainder - assert 2 == 3\n"
        "4 failed, 7 passed in 0.09s\r\n"
    )
    outcome = parse_test_output(output, "", 1)
    assert outcome.parsed
    assert outcome.passed == 7
    assert outcome.failed == 4
    assert not outcome.green
    assert outcome.duration_s == pytest.approx(0.09)


def test_parses_quiet_mode_success() -> None:
    outcome = parse_test_output("...........   [100%]\n11 passed in 0.08s\r\n", "", 0)
    assert outcome.parsed
    assert outcome.green
    assert outcome.passed == 11


def test_parses_a_summary_with_warnings() -> None:
    outcome = parse_test_output("3 passed, 1 warning in 0.12s\n", "", 0)
    assert outcome.green
    assert outcome.passed == 3


def test_no_tests_ran_is_not_green() -> None:
    outcome = parse_test_output("no tests ran in 0.01s\n", "", 5)
    assert outcome.parsed
    assert not outcome.green


def test_parses_a_passing_pytest_run() -> None:
    outcome = parse_test_output("collected 185 items\n\n===== 185 passed in 3.9s =====\n", "", 0)
    assert outcome.green
    assert outcome.passed == 185
    assert outcome.failed == 0


def test_parses_pytest_collection_errors() -> None:
    outcome = parse_test_output("==== 1 error in 0.2s ====\n", "", 2)
    assert outcome.parsed
    assert outcome.errors == 1
    assert not outcome.green


def test_parses_unittest_output() -> None:
    outcome = parse_test_output(
        "", "Ran 12 tests in 0.501s\n\nFAILED (failures=2, errors=1)\n", 1
    )
    assert outcome.framework == "unittest"
    assert outcome.total == 12
    assert outcome.failed == 2
    assert outcome.errors == 1
    assert outcome.passed == 9
    assert not outcome.green


def test_parses_passing_unittest_output() -> None:
    outcome = parse_test_output("", "Ran 5 tests in 0.1s\n\nOK\n", 0)
    assert outcome.green
    assert outcome.passed == 5


def test_unparsed_output_falls_back_to_the_exit_code() -> None:
    assert parse_test_output("weird output", "", 0).green
    assert not parse_test_output("weird output", "", 1).green


def test_a_timeout_is_never_green() -> None:
    outcome = parse_test_output("===== 10 passed in 1s =====", "", 0, timed_out=True)
    assert not outcome.green


def test_failure_excerpt_prefers_the_failures_section() -> None:
    text = "noise\n" * 500 + "=== FAILURES ===\nassert 1 == 2\n"
    excerpt = extract_failure_excerpt(text, "", limit=1000)
    assert excerpt.startswith("=== FAILURES ===")
    assert "assert 1 == 2" in excerpt


def test_failure_excerpt_falls_back_to_the_tail() -> None:
    excerpt = extract_failure_excerpt("a" * 10_000, "", limit=500)
    assert len(excerpt) < 700
    assert "head truncated" in excerpt
