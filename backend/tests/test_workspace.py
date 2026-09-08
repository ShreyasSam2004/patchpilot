"""Workspace path-jail and git tests."""

from __future__ import annotations

import pytest

from patchpilot.errors import PathEscapeError, ToolError
from patchpilot.workspace.workspace import Workspace


@pytest.mark.parametrize(
    "path",
    [
        "../secrets.txt",
        "../../etc/passwd",
        "src/../../outside.py",
        "/etc/passwd",
        "C:/Windows/System32/config",
        "src/../../../..",
    ],
)
def test_paths_outside_the_root_are_refused(workspace: Workspace, path: str) -> None:
    with pytest.raises(PathEscapeError):
        workspace.resolve(path)


@pytest.mark.parametrize("path", ["src/calc.py", "./src/calc.py", "src//calc.py", "README.md"])
def test_paths_inside_the_root_resolve(workspace: Workspace, path: str) -> None:
    resolved = workspace.resolve(path)
    assert workspace.root in resolved.parents


def test_root_itself_resolves(workspace: Workspace) -> None:
    assert workspace.resolve(".") == workspace.root
    assert workspace.resolve("") == workspace.root


def test_missing_path_with_must_exist_raises(workspace: Workspace) -> None:
    with pytest.raises(ToolError):
        workspace.resolve("nope.py", must_exist=True)


def test_read_text_returns_content(workspace: Workspace) -> None:
    content, truncated = workspace.read_text("src/calc.py", 100_000)
    assert "def divide" in content
    assert not truncated
    assert "src/calc.py" in workspace.files_read


def test_read_text_truncates_at_the_budget(workspace: Workspace) -> None:
    workspace.write_text("big.txt", "y" * 5000)
    content, truncated = workspace.read_text("big.txt", 1000)
    assert truncated
    assert len(content) == 1000


def test_binary_files_are_refused(workspace: Workspace) -> None:
    (workspace.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    with pytest.raises(ToolError):
        workspace.read_text("image.png", 100_000)


def test_list_dir_skips_vendor_directories(workspace: Workspace) -> None:
    (workspace.root / "node_modules").mkdir()
    (workspace.root / "node_modules" / "x.js").write_text("1", encoding="utf-8")
    entries = workspace.list_dir(".", depth=2)
    assert not any(e == "node_modules/x.js" for e in entries)
    assert "src/" in entries
    assert "src/calc.py" in entries


async def test_diff_reports_modifications(workspace: Workspace) -> None:
    workspace.write_text("src/calc.py", "def add(a, b):\n    return a + b + 1\n")
    diff = await workspace.diff()
    assert "src/calc.py" in diff
    assert "+    return a + b + 1" in diff

    stat = await workspace.diff_stat()
    assert stat.files_changed == 1
    assert stat.insertions >= 1


async def test_diff_includes_new_files(workspace: Workspace) -> None:
    workspace.write_text("src/new_module.py", "VALUE = 1\n")
    diff = await workspace.diff()
    assert "src/new_module.py" in diff
    assert "+VALUE = 1" in diff


async def test_diff_is_empty_on_a_clean_tree(workspace: Workspace) -> None:
    assert (await workspace.diff()).strip() == ""


async def test_apply_patch_round_trips(workspace: Workspace) -> None:
    original = "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a / b\n"
    patched = (
        "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n"
        "    if b == 0:\n        return None\n    return a / b\n"
    )
    workspace.write_text("src/calc.py", patched)
    diff = await workspace.diff()
    workspace.write_text("src/calc.py", original)
    await workspace.git("reset", "--quiet")

    result = await workspace.apply_patch(diff)
    assert result.ok, result.stderr
    assert "return None" in workspace.resolve("src/calc.py").read_text(encoding="utf-8")


async def test_apply_patch_rejects_a_stale_hunk(workspace: Workspace) -> None:
    bad = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-this line is not in the file\n"
        "+replacement\n"
        " context\n"
    )
    result = await workspace.apply_patch(bad)
    assert not result.ok


async def test_reset_hard_discards_everything(workspace: Workspace) -> None:
    workspace.write_text("src/calc.py", "broken\n")
    workspace.write_text("junk.py", "junk\n")
    await workspace.reset_hard()
    assert "def add" in workspace.resolve("src/calc.py").read_text(encoding="utf-8")
    assert not (workspace.root / "junk.py").exists()


async def test_git_info(workspace: Workspace) -> None:
    info = await workspace.info()
    assert info.head and len(info.head) == 40
    assert info.dirty is False
