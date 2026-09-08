"""Native tool implementations."""

from ..base import Tool
from .edit import ApplyPatchTool, EditFileTool
from .exec import RunCommandTool, RunTestsTool
from .fs import ListFilesTool, ReadFileTool, WriteFileTool
from .git import GitDiffTool, GitLogTool, GitStatusTool
from .meta import FinishTool, RecordHypothesisTool
from .search import FindSymbolTool, SearchCodeTool
from .testparse import TestOutcome, extract_failure_excerpt, parse_test_output

__all__ = [
    "ApplyPatchTool",
    "EditFileTool",
    "FindSymbolTool",
    "FinishTool",
    "GitDiffTool",
    "GitLogTool",
    "GitStatusTool",
    "ListFilesTool",
    "ReadFileTool",
    "RecordHypothesisTool",
    "RunCommandTool",
    "RunTestsTool",
    "SearchCodeTool",
    "TestOutcome",
    "WriteFileTool",
    "build_native_tools",
    "extract_failure_excerpt",
    "parse_test_output",
]


def build_native_tools() -> list[Tool]:
    """Every native tool, in the order the agent should think about them."""
    return [
        ListFilesTool(),
        SearchCodeTool(),
        FindSymbolTool(),
        ReadFileTool(),
        RecordHypothesisTool(),
        ApplyPatchTool(),
        EditFileTool(),
        WriteFileTool(),
        GitDiffTool(),
        GitStatusTool(),
        GitLogTool(),
        RunTestsTool(),
        RunCommandTool(),
        FinishTool(),
    ]
