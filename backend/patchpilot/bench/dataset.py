"""SWE-bench dataset loading.

Instances come from HuggingFace (``princeton-nlp/SWE-bench_Lite`` by default).
The ``datasets`` package is an optional dependency, so this module reports a
clear, actionable error instead of failing at import time — and supports a
local JSON/JSONL file so the pipeline can be exercised offline.

Field names follow the dataset schema:
``instance_id, repo, base_commit, problem_statement, patch, test_patch,
FAIL_TO_PASS, PASS_TO_PASS, environment_setup_commit, version, hints_text``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import BenchmarkError
from ..logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
DEFAULT_SPLIT = "test"

#: The dataset moved organisations; both names refer to the same data. We try
#: the configured name first and fall back, so an older config keeps working.
DATASET_ALIASES: dict[str, list[str]] = {
    "princeton-nlp/SWE-bench_Lite": ["princeton-nlp/SWE-bench_Lite", "SWE-bench/SWE-bench_Lite"],
    "SWE-bench/SWE-bench_Lite": ["SWE-bench/SWE-bench_Lite", "princeton-nlp/SWE-bench_Lite"],
    "princeton-nlp/SWE-bench_Verified": [
        "princeton-nlp/SWE-bench_Verified",
        "SWE-bench/SWE-bench_Verified",
    ],
    "princeton-nlp/SWE-bench": ["princeton-nlp/SWE-bench", "SWE-bench/SWE-bench"],
}


@dataclass(slots=True)
class SweBenchInstance:
    """One SWE-bench task instance."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str = ""
    test_patch: str = ""
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    environment_setup_commit: str | None = None
    version: str = ""
    hints_text: str = ""

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    @property
    def test_files(self) -> list[str]:
        """Test files touched by FAIL_TO_PASS, used to suggest a test command."""
        files: list[str] = []
        for node_id in self.fail_to_pass:
            path = node_id.split("::")[0]
            if path.endswith(".py") and path not in files:
                files.append(path)
        return files

    def suggested_test_command(self) -> str:
        targets = " ".join(self.test_files[:4])
        return f"python -m pytest {targets} -rA --tb=short" if targets else "python -m pytest -q"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "fail_to_pass": self.fail_to_pass,
            "pass_to_pass": self.pass_to_pass,
            "environment_setup_commit": self.environment_setup_commit,
            "version": self.version,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> SweBenchInstance:
        return cls(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row.get("problem_statement", ""),
            patch=row.get("patch", "") or "",
            test_patch=row.get("test_patch", "") or "",
            fail_to_pass=_as_list(row.get("FAIL_TO_PASS") or row.get("fail_to_pass")),
            pass_to_pass=_as_list(row.get("PASS_TO_PASS") or row.get("pass_to_pass")),
            environment_setup_commit=row.get("environment_setup_commit"),
            version=str(row.get("version", "")),
            hints_text=row.get("hints_text", "") or "",
        )


def _as_list(value: Any) -> list[str]:
    """FAIL_TO_PASS/PASS_TO_PASS are JSON-encoded strings in some releases."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else [value]
        except json.JSONDecodeError:
            return [value]
    return []


def load_instances(
    dataset: str = DEFAULT_DATASET,
    split: str = DEFAULT_SPLIT,
    *,
    instance_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[SweBenchInstance]:
    """Load instances from HuggingFace or a local JSON/JSONL file."""
    local = Path(dataset)
    rows = (
        _load_local(local)
        if local.exists() and local.suffix in {".json", ".jsonl"}
        else _load_huggingface(dataset, split)
    )

    instances = [SweBenchInstance.from_row(row) for row in rows]

    if instance_ids:
        wanted = list(dict.fromkeys(instance_ids))
        by_id = {i.instance_id: i for i in instances}
        missing = [i for i in wanted if i not in by_id]
        if missing:
            raise BenchmarkError(
                f"instance ids not found in {dataset}: {', '.join(missing[:5])}"
                + (f" (+{len(missing) - 5} more)" if len(missing) > 5 else "")
            )
        return [by_id[i] for i in wanted]

    # Deterministic ordering so a given (offset, limit) always selects the same
    # instances — required for reproducing a benchmark run from its config.
    instances.sort(key=lambda i: i.instance_id)
    selected = instances[offset:]
    return selected[:limit] if limit else selected


def _load_local(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    data = json.loads(text)
    return data if isinstance(data, list) else data.get("instances", [])


def _load_huggingface(dataset: str, split: str) -> list[dict[str, Any]]:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise BenchmarkError(
            "loading SWE-bench requires the 'datasets' package. Install the benchmark "
            "extras:  pip install -e '.[swebench]'   — or point "
            "PATCHPILOT_SWEBENCH_DATASET at a local .json/.jsonl file."
        ) from exc

    names = DATASET_ALIASES.get(dataset, [dataset])
    last_error: Exception | None = None
    for name in names:
        try:
            log.info("swebench.loading", dataset=name, split=split)
            data = load_dataset(name, split=split)
            return [dict(row) for row in data]
        except Exception as exc:
            last_error = exc
            log.warning("swebench.load_failed", dataset=name, error=str(exc)[:200])

    raise BenchmarkError(
        f"could not load dataset {dataset!r} (split {split!r}): {last_error}. "
        "Check the dataset name and your network access to huggingface.co."
    )


def dataset_available() -> tuple[bool, str]:
    """Whether the dataset dependency is installed.

    Uses ``find_spec`` rather than importing: ``import datasets`` pulls in
    pyarrow and pandas and costs ~4 seconds, which is absurd for a yes/no
    answer that every page load asks for.
    """
    import importlib.util

    try:
        found = importlib.util.find_spec("datasets") is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        return False, "the 'datasets' package is not installed (pip install -e '.[swebench]')"
    return True, "datasets package available"
