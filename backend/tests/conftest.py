"""Shared test fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("PATCHPILOT_ENVIRONMENT", "test")
os.environ.setdefault("PATCHPILOT_PROVIDER", "scripted")
os.environ.setdefault("PATCHPILOT_SANDBOX", "local")

from patchpilot.config import Settings
from patchpilot.sandbox.base import SandboxLimits
from patchpilot.sandbox.local import LocalSandbox
from patchpilot.tools.base import ToolContext
from patchpilot.tools.native import build_native_tools
from patchpilot.tools.policy import ToolPolicy
from patchpilot.tools.registry import ToolRegistry
from patchpilot.workspace.workspace import Workspace, prepare_workspace

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        provider="scripted",
        sandbox="local",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        max_test_runtime_s=120,
        command_timeout_s=120,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> LocalSandbox:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    return LocalSandbox(root, SandboxLimits(timeout_s=60, max_output_bytes=200_000))


@pytest.fixture
async def workspace(tmp_path: Path, sandbox: LocalSandbox) -> Workspace:
    """A tiny git repository with one Python file."""
    root = sandbox.root
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    workspace = Workspace(root, sandbox)
    await workspace.git("init", "--quiet")
    await workspace.ensure_git_identity()
    await workspace.git("add", "-A")
    await workspace.git("commit", "--quiet", "-m", "initial")
    return workspace


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry(policy=ToolPolicy.permissive())
    reg.register_all(build_native_tools())
    return reg


@pytest.fixture
def tool_ctx(workspace: Workspace, sandbox: LocalSandbox, settings: Settings) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        sandbox=sandbox,
        settings=settings,
        emit=None,
        state={"test_command": f"{sys.executable} -m pytest -q"},
    )


@pytest.fixture
async def fixture_workspace(request, tmp_path: Path):
    """Prepare one of backend/fixtures/* as a real git workspace."""
    name = getattr(request, "param", "calculator_bug")
    root = tmp_path / "fixture_repo"
    sandbox = LocalSandbox(root, SandboxLimits(timeout_s=120, max_output_bytes=200_000))
    workspace = await prepare_workspace(root, sandbox, local_path=FIXTURES_DIR / name / "repo")
    return workspace
