"""Application configuration.

All configuration is read from the environment (optionally via a ``.env`` file).
Nothing in this module ever writes a secret to a log or an API response; see
``Settings.redacted()`` for the representation that is safe to expose.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent

SandboxKind = Literal["local", "docker"]
ProviderKind = Literal["anthropic", "openai", "scripted"]


class Settings(BaseSettings):
    """Runtime settings for the API, worker, agent and benchmark runner."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="PATCHPILOT_",
        extra="ignore",
    )

    # ---- general -----------------------------------------------------------
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # ---- persistence -------------------------------------------------------
    database_url: str = Field(
        default_factory=lambda: "sqlite+aiosqlite:///"
        + (BACKEND_ROOT / "data" / "patchpilot.db").as_posix()
    )
    data_dir: Path = BACKEND_ROOT / "data"
    workspace_root: Path = BACKEND_ROOT / "workspaces"

    # ---- api ---------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # ---- worker ------------------------------------------------------------
    worker_enabled: bool = True
    """Run the job worker inside the API process (convenient for local dev)."""
    worker_concurrency: int = 2
    worker_poll_interval_s: float = 0.5

    # ---- model provider ----------------------------------------------------
    provider: ProviderKind = "anthropic"
    model: str = "claude-sonnet-5"
    max_output_tokens: int = 8192
    temperature: float = 0.0
    """OpenAI only. The current Claude models reject sampling parameters; use
    `effort` instead."""
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    """Anthropic reasoning depth / token spend. 'high' is the API default;
    'xhigh' suits hard agentic coding work at higher cost."""
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None
    openai_api_key: str | None = None
    openai_base_url: str | None = None

    # ---- agent budgets -----------------------------------------------------
    max_iterations: int = 30
    max_tool_calls: int = 160
    max_runtime_s: int = 1800
    max_test_runtime_s: int = 600
    max_cost_usd: float = 5.0
    max_tool_output_bytes: int = 24_000
    max_file_read_bytes: int = 120_000

    # ---- sandbox -----------------------------------------------------------
    sandbox: SandboxKind = "local"
    docker_image: str = "patchpilot/runner:latest"
    """Image for the Docker sandbox. It runs with --network none, so nothing can
    be installed at run time: the image must already contain git, the test
    runner, and the repository's dependencies. Build the bundled one with
    `make docker-image`, or derive from it for a project with extra deps."""
    docker_memory: str = "2g"
    docker_cpus: str = "2"
    docker_pids_limit: int = 512
    docker_network: str = "none"
    command_timeout_s: int = 300

    # ---- tools / mcp -------------------------------------------------------
    tool_allow: list[str] = [
        "list_files",
        "read_file",
        "search_code",
        "find_symbol",
        "apply_patch",
        "edit_file",
        "write_file",
        "git_diff",
        "git_status",
        "git_log",
        "run_tests",
        "run_command",
        "record_hypothesis",
        "finish",
    ]
    tool_deny: list[str] = [
        "git_push",
        "git_commit",
        "delete_repository",
        "host_shell",
        "credential_access",
        "network_fetch",
    ]
    mcp_enabled: bool = False
    mcp_config_path: Path = REPO_ROOT / "mcp.config.json"

    # ---- benchmark ---------------------------------------------------------
    swebench_dataset: str = "princeton-nlp/SWE-bench_Lite"
    swebench_split: str = "test"
    swebench_run_root: Path = BACKEND_ROOT / "data" / "swebench"
    swebench_max_workers: int = 4
    swebench_eval_timeout_s: int = 3600
    swebench_instance_images: bool = True
    """Run each benchmark task in the official per-instance SWE-bench image, so
    the agent can actually run that repository's tests. Requires the docker
    sandbox; each image is multiple GB and is pulled on first use."""
    swebench_image_namespace: str = "swebench"
    swebench_image_tag: str = "latest"
    swebench_python: str | None = None
    """Interpreter used to invoke the official harness. Defaults to sys.executable."""

    # ---- validators --------------------------------------------------------
    @field_validator("cors_origins", "tool_allow", "tool_deny", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value  # let pydantic parse JSON
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("data_dir", "workspace_root", "swebench_run_root")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BACKEND_ROOT / value).resolve()

    # ---- helpers -----------------------------------------------------------
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def api_key_for(self, provider: str) -> str | None:
        """Resolve a provider key, falling back to the vendor's standard env var."""
        if provider == "anthropic":
            return self.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        if provider == "openai":
            return self.openai_api_key or os.environ.get("OPENAI_API_KEY")
        return None

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workspace_root, self.swebench_run_root):
            path.mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict[str, Any]:
        """Configuration safe to return from the API or write to a log."""
        secret_fields = {"anthropic_api_key", "openai_api_key"}
        out: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name in secret_fields:
                out[name] = "set" if self.api_key_for(name.split("_")[0]) else None
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch environment variables."""
    get_settings.cache_clear()
