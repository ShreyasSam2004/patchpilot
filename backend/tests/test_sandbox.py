"""Sandbox isolation tests."""

from __future__ import annotations

import os
import sys

import pytest

from patchpilot.errors import PathEscapeError
from patchpilot.sandbox.base import build_env
from patchpilot.sandbox.guard import check_command
from patchpilot.sandbox.local import LocalSandbox


async def test_runs_a_command_and_captures_output(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec([sys.executable, "-c", "print('hello')"])
    assert result.ok
    assert "hello" in result.stdout
    assert result.exit_code == 0


async def test_non_zero_exit_is_reported_not_raised(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert not result.ok
    assert result.exit_code == 3


async def test_stderr_is_captured(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom')"]
    )
    assert "boom" in result.stderr


async def test_timeout_kills_the_process(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=2
    )
    assert result.timed_out
    assert not result.ok
    assert "exceeded" in result.stderr


async def test_output_is_capped(sandbox: LocalSandbox) -> None:
    sandbox.limits.max_output_bytes = 2000
    result = await sandbox.exec(
        [sys.executable, "-c", "print('x' * 100000)"], timeout_s=30
    )
    assert result.truncated
    assert len(result.stdout) <= 2100


async def test_missing_executable_returns_127(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec(["definitely-not-a-real-binary-xyz"])
    assert result.exit_code == 127
    assert "not found" in result.stderr


async def test_cwd_outside_root_is_refused(sandbox: LocalSandbox) -> None:
    with pytest.raises(PathEscapeError):
        await sandbox.exec([sys.executable, "-c", "pass"], cwd="../../etc")


async def test_secrets_are_not_inherited(sandbox: LocalSandbox, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_DB_PASSWORD", "hunter2")
    result = await sandbox.exec(
        [sys.executable, "-c", "import os,json; print(json.dumps(dict(os.environ)))"]
    )
    assert result.ok
    assert "sk-should-not-leak" not in result.stdout
    assert "hunter2" not in result.stdout


def test_build_env_drops_secret_shaped_variables(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    env = build_env()
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "PATH" in env


def test_build_env_extra_cannot_smuggle_secrets() -> None:
    env = build_env({"SAFE_VAR": "1", "SNEAKY_TOKEN": "abc"})
    assert env["SAFE_VAR"] == "1"
    assert "SNEAKY_TOKEN" not in env


@pytest.mark.parametrize(
    "argv",
    [
        ["sudo", "rm", "-rf", "/"],
        ["ssh", "user@host"],
        ["curl", "https://evil.example/x"],
        ["git", "push", "origin", "main"],
        ["git", "config", "--global", "user.email", "x"],
        ["docker", "run", "-v", "/:/host", "alpine"],
        ["python", "-c", "print(1)", "&&", "rm"],
        ["cat", "~/.ssh/id_rsa"],
        ["cat", "/home/u/.aws/credentials"],
        ["rm", "-rf", "."],
        ["pytest", "$(whoami)"],
    ],
)
def test_guard_refuses_dangerous_commands(argv: list[str]) -> None:
    verdict = check_command(argv)
    assert not verdict.allowed, f"{argv} should have been refused"
    assert verdict.reason


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "pytest", "tests/test_token.py", "-x"],
        ["pytest", "-q", "--no-header"],
        ["git", "diff", "--cached"],
        ["git", "log", "-n5"],
        ["npm", "test"],
        ["tox", "-e", "py311"],
        ["ruff", "check", "."],
    ],
)
def test_guard_allows_ordinary_developer_commands(argv: list[str]) -> None:
    assert check_command(argv).allowed, f"{argv} should have been allowed"


def test_guard_allows_network_when_explicitly_enabled() -> None:
    assert not check_command(["curl", "https://x"]).allowed
    assert check_command(["curl", "https://x"], allow_network=True).allowed


async def test_working_directory_is_the_workspace(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec([sys.executable, "-c", "import os; print(os.getcwd())"])
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(sandbox.root))


def test_info_admits_its_limits(sandbox: LocalSandbox) -> None:
    info = sandbox.info()
    assert info.kind == "local"
    assert info.isolation == "process"
    assert any("kernel-level" in warning for warning in info.warnings)


# ------------------------------------------------- interpreter normalisation


def test_bare_python_maps_to_the_running_interpreter(sandbox: LocalSandbox) -> None:
    """A bare `python` on PATH is often a different interpreter than ours.

    Without this, `python -m pytest` — the single most common test command —
    fails with "No module named pytest" whenever PatchPilot runs from a
    virtualenv, and the agent can never validate a fix.
    """
    for name in ("python", "python3", "py"):
        assert sandbox.normalize_argv([name, "-m", "pytest"]) == [
            sys.executable,
            "-m",
            "pytest",
        ]


def test_an_explicit_interpreter_path_is_left_alone(sandbox: LocalSandbox) -> None:
    argv = ["/usr/bin/python3", "-c", "pass"]
    assert sandbox.normalize_argv(argv) == argv
    argv = [r"C:\Python312\python.exe", "-c", "pass"]
    assert sandbox.normalize_argv(argv) == argv


def test_other_executables_are_untouched(sandbox: LocalSandbox) -> None:
    for argv in (["npm", "test"], ["tox", "-e", "py311"], ["git", "diff"]):
        assert sandbox.normalize_argv(argv) == argv


async def test_bare_python_actually_runs_our_interpreter(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec(["python", "-c", "import sys; print(sys.executable)"])
    assert result.ok
    assert result.stdout.strip() == sys.executable


async def test_bare_python_can_import_pytest(sandbox: LocalSandbox) -> None:
    result = await sandbox.exec(["python", "-m", "pytest", "--version"])
    assert result.ok, result.combined_output
