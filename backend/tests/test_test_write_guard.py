"""Refusing to let a run edit the tests it is judged by.

Why this exists. `classify_write` labelled test writes and `RepairProgress`
counted them, but nothing ever refused one. On a benchmark that is a hole in the
measurement rather than a style problem: FAIL_TO_PASS and PASS_TO_PASS come from
the harness, so an agent free to edit them can turn any instance green without
fixing anything, and `verified_by_tests` stops meaning what it says.

It is deliberately not the default. On a real repository a regression test
beside the fix is what a good contributor writes, so there the edit is shown to
the reviewer instead of being blocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from patchpilot.errors import ToolError
from patchpilot.repair.progress import refuse_test_writes
from patchpilot.workspace.workspace import Workspace


class _Sandbox:
    """The workspace only needs a sandbox for git; these tests never reach it."""


def _workspace(tmp_path: Path, *, guarded: bool) -> Workspace:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "tests").mkdir(exist_ok=True)
    return Workspace(
        tmp_path,
        _Sandbox(),  # type: ignore[arg-type]
        write_guard=refuse_test_writes if guarded else None,
    )


# --- the policy itself --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_thing.py",
        "test/test_thing.py",
        "src/pkg/tests/test_thing.py",
        "test_toplevel.py",
        "pkg/thing_test.py",
        "testing/test_helper.py",
        # Resolve `..` before classifying, as the workspace does before writing:
        # a scratch-looking prefix must not smuggle a write onto a test file.
        ".patchpilot/../tests/test_thing.py",
        "./.patchpilot/../test_toplevel.py",
        "src/../tests/test_thing.py",
    ],
)
def test_a_test_path_is_refused(path: str) -> None:
    reason = refuse_test_writes(path)
    assert reason is not None
    assert path in reason
    assert "implementation" in reason


@pytest.mark.parametrize(
    "path",
    [
        "src/pkg/module.py",
        "tabulate/__init__.py",
        "README.md",
        "contest.py",  # contains "test" but is not a test path
        "latest_release.py",
    ],
)
def test_source_paths_are_allowed(path: str) -> None:
    assert refuse_test_writes(path) is None


# --- the workspace enforces it ------------------------------------------------


def test_write_text_refuses_a_test_file(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, guarded=True)
    with pytest.raises(ToolError) as caught:
        workspace.write_text("tests/test_thing.py", "def test_x(): assert True\n")
    assert caught.value.code == "write_refused"
    assert not (tmp_path / "tests" / "test_thing.py").exists(), "nothing may be written"


def test_a_dotdot_path_through_scratch_space_cannot_reach_a_test(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, guarded=True)
    with pytest.raises(ToolError) as caught:
        workspace.write_text(".patchpilot/../tests/test_thing.py", "def test_x(): pass\n")
    assert caught.value.code == "write_refused"
    assert not (tmp_path / "tests" / "test_thing.py").exists()
    # Scratch space itself is still writable.
    workspace.write_text(".patchpilot/repro.py", "print(1)\n")


def test_write_text_still_allows_source(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, guarded=True)
    workspace.write_text("src/module.py", "value = 1\n")
    assert (tmp_path / "src" / "module.py").read_text(encoding="utf-8") == "value = 1\n"


def test_an_unguarded_workspace_is_unchanged(tmp_path: Path) -> None:
    """Ordinary runs keep writing tests; only the benchmark path is closed."""
    workspace = _workspace(tmp_path, guarded=False)
    workspace.write_text("tests/test_thing.py", "def test_x(): assert True\n")
    assert (tmp_path / "tests" / "test_thing.py").exists()


async def test_apply_patch_refuses_a_diff_that_touches_tests(tmp_path: Path) -> None:
    """The diff path must be guarded too, or the block is trivially bypassed."""
    workspace = _workspace(tmp_path, guarded=True)
    diff = (
        "diff --git a/tests/test_thing.py b/tests/test_thing.py\n"
        "--- a/tests/test_thing.py\n"
        "+++ b/tests/test_thing.py\n"
        "@@ -1 +1 @@\n"
        "-assert False\n"
        "+assert True\n"
    )
    with pytest.raises(ToolError) as caught:
        await workspace.apply_patch(diff)
    assert caught.value.code == "write_refused"
    assert not (tmp_path / ".patchpilot_patch.diff").exists(), "refused before writing"


async def test_a_mixed_patch_is_refused_whole(tmp_path: Path) -> None:
    """git applies all or nothing, so one guarded path stops the entire patch."""
    workspace = _workspace(tmp_path, guarded=True)
    diff = (
        "diff --git a/src/module.py b/src/module.py\n"
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
        "diff --git a/tests/test_thing.py b/tests/test_thing.py\n"
        "--- a/tests/test_thing.py\n"
        "+++ b/tests/test_thing.py\n"
        "@@ -1 +1 @@\n"
        "-assert False\n"
        "+assert True\n"
    )
    with pytest.raises(ToolError) as caught:
        await workspace.apply_patch(diff)
    assert caught.value.code == "write_refused"
    assert "tests/test_thing.py" in str(caught.value)


async def test_a_source_only_patch_is_not_blocked_by_the_guard(tmp_path: Path) -> None:
    """The guard must let a normal repair through; it fails later for lack of git."""
    workspace = _workspace(tmp_path, guarded=True)
    diff = (
        "diff --git a/src/module.py b/src/module.py\n"
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )
    with pytest.raises(Exception) as caught:
        await workspace.apply_patch(diff)
    assert not isinstance(caught.value, ToolError) or caught.value.code != "write_refused"


# --- benchmark runs opt in, ordinary runs do not ------------------------------


def test_benchmark_tasks_protect_their_tests() -> None:
    from patchpilot.bench.dataset import SweBenchInstance
    from patchpilot.bench.runner import build_agent_task

    instance = SweBenchInstance(
        instance_id="astropy__astropy-12907",
        repo="astropy/astropy",
        base_commit="a" * 40,
        problem_statement="something is wrong",
    )
    task = build_agent_task(instance, "run-1")
    assert task.protect_tests is True


def test_an_ordinary_task_does_not() -> None:
    from patchpilot.agent.orchestrator import AgentTask

    assert AgentTask(run_id="r", issue="i").protect_tests is False
