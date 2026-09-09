"""Official SWE-bench evaluation adapter.

PatchPilot does **not** grade its own patches. Grading means: check out the
instance, apply the model patch, apply the *test* patch, run FAIL_TO_PASS and
PASS_TO_PASS, and decide. Re-implementing that would produce a number that
looks like a SWE-bench score but is not one.

So this module shells out to the official harness:

    python -m swebench.harness.run_evaluation \\
        --dataset_name princeton-nlp/SWE-bench_Lite \\
        --predictions_path predictions.jsonl \\
        --max_workers N --run_id <run_key>

and then parses the harness's own report files. Everything reported in the UI
as "resolved" comes from those files.

Requirements the harness imposes, and which we check up front rather than
failing halfway through a batch:
  * the ``swebench`` package installed;
  * a running Docker daemon;
  * substantial free disk (the docs recommend ~120 GB for a full run).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..domain.enums import BenchmarkStatus
from ..logging_setup import get_logger
from ..sandbox.docker import docker_available

log = get_logger(__name__)

HARNESS_MODULE = "swebench.harness.run_evaluation"


@dataclass(slots=True)
class Prediction:
    """One line of the predictions file, in the schema the harness expects."""

    instance_id: str
    model_name_or_path: str
    model_patch: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "instance_id": self.instance_id,
                "model_name_or_path": self.model_name_or_path,
                "model_patch": self.model_patch,
            },
            ensure_ascii=False,
        )


@dataclass
class EvaluationOutcome:
    """What the official harness concluded."""

    ok: bool
    resolved_ids: set[str] = field(default_factory=set)
    unresolved_ids: set[str] = field(default_factory=set)
    error_ids: set[str] = field(default_factory=set)
    empty_patch_ids: set[str] = field(default_factory=set)
    per_instance: dict[str, dict[str, Any]] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    report_paths: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    def status_for(self, instance_id: str) -> BenchmarkStatus:
        if instance_id in self.resolved_ids:
            return BenchmarkStatus.RESOLVED
        if instance_id in self.empty_patch_ids:
            return BenchmarkStatus.EMPTY_PATCH
        if instance_id in self.error_ids:
            return BenchmarkStatus.ERROR
        if instance_id in self.unresolved_ids:
            return BenchmarkStatus.UNRESOLVED
        return BenchmarkStatus.NOT_EVALUATED


def write_predictions(path: Path, predictions: list[Prediction]) -> Path:
    """Write the JSONL predictions file the harness reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(p.to_json() for p in predictions) + "\n", encoding="utf-8", newline="\n"
    )
    log.info("swebench.predictions_written", path=str(path), count=len(predictions))
    return path


def required_disk_gb(instance_count: int | None) -> float:
    """Disk the harness plausibly needs for a batch of this size.

    The ~120 GB in the official docs is for the *full* 300-instance run. A
    small batch needs far less, and instances from the same repository share
    almost all of their layers -- measured here: the first sympy image cost
    3.6 GB, the second 288 MB, the third 87 MB. Blocking a 15-instance batch
    because the machine lacks 120 GB would refuse work that comfortably fits.
    """
    if instance_count is None:
        return 50.0
    return max(10.0, 5.0 + 0.6 * instance_count)


async def evaluator_preflight(
    settings: Settings, instance_count: int | None = None
) -> dict[str, Any]:
    """Check every prerequisite the harness has, before spending money."""
    problems: list[str] = []

    harness_installed = _harness_installed()
    if not harness_installed:
        problems.append(
            "the 'swebench' package is not installed — run: pip install -e '.[swebench]'"
        )

    docker_ok, docker_detail = await docker_available()
    if not docker_ok:
        problems.append(
            f"Docker is required by the SWE-bench harness but is unavailable: {docker_detail}"
        )

    free_gb = _free_disk_gb(settings.swebench_run_root)
    needed = required_disk_gb(instance_count)
    notes: list[str] = []
    if free_gb is not None and free_gb < needed:
        scope = (
            f"{instance_count} instances" if instance_count else "a full run"
        )
        problems.append(
            f"only {free_gb:.0f} GB free on the SWE-bench working volume; "
            f"evaluating {scope} needs roughly {needed:.0f} GB."
        )
    elif free_gb is not None and free_gb < 120:
        # Enough for this batch, but worth saying out loud.
        notes.append(
            f"{free_gb:.0f} GB free. Enough for this batch; the official docs "
            "recommend ~120 GB for the full 300-instance run."
        )

    return {
        "harness_installed": harness_installed,
        "docker_available": docker_ok,
        "docker_detail": docker_detail,
        "free_disk_gb": round(free_gb, 1) if free_gb is not None else None,
        "required_disk_gb": needed,
        "instance_count": instance_count,
        "ready": not problems,
        "problems": problems,
        "notes": notes,
    }


def _harness_installed() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("swebench") is not None
    except (ImportError, ValueError):
        return False


def _free_disk_gb(path: Path) -> float | None:
    try:
        target = path if path.exists() else path.parent
        target.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(target).free / (1024**3)
    except OSError:
        return None


async def run_official_evaluation(
    *,
    predictions_path: Path,
    run_key: str,
    dataset: str,
    split: str,
    model_name: str,
    settings: Settings,
    instance_ids: list[str] | None = None,
    working_dir: Path | None = None,
) -> EvaluationOutcome:
    """Invoke the official harness and parse its report."""
    preflight = await evaluator_preflight(settings)
    if not preflight["harness_installed"] or not preflight["docker_available"]:
        return EvaluationOutcome(
            ok=False,
            error="; ".join(preflight["problems"]),
            summary={"preflight": preflight},
        )

    cwd = working_dir or settings.swebench_run_root
    cwd.mkdir(parents=True, exist_ok=True)
    python = settings.swebench_python or sys.executable

    argv = [
        python,
        "-m",
        HARNESS_MODULE,
        "--dataset_name", dataset,
        "--split", split,
        "--predictions_path", str(predictions_path),
        "--max_workers", str(settings.swebench_max_workers),
        "--run_id", run_key,
    ]
    if instance_ids:
        argv += ["--instance_ids", *instance_ids]

    log.info(
        "swebench.evaluating",
        run_key=run_key,
        instances=len(instance_ids or []),
        cwd=str(cwd),
    )

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(), timeout=settings.swebench_eval_timeout_s
        )
    except TimeoutError:
        proc.kill()
        return EvaluationOutcome(
            ok=False,
            error=(
                f"the SWE-bench harness exceeded "
                f"{settings.swebench_eval_timeout_s}s and was killed"
            ),
        )

    stdout = out_b.decode("utf-8", "replace")
    stderr = err_b.decode("utf-8", "replace")
    exit_code = proc.returncode or 0

    outcome = parse_evaluation_report(cwd, run_key, model_name)
    outcome.stdout_tail = stdout[-8000:]
    outcome.stderr_tail = stderr[-8000:]

    if not outcome.per_instance and exit_code != 0:
        outcome.ok = False
        outcome.error = (
            f"the harness exited with code {exit_code} and produced no report. "
            f"stderr: {stderr.strip()[-1500:]}"
        )
    elif not outcome.per_instance:
        outcome.ok = False
        outcome.error = (
            "the harness produced no per-instance report; check "
            f"{cwd / 'logs' / 'evaluation' / run_key}"
        )
    else:
        outcome.ok = True

    log.info(
        "swebench.evaluated",
        run_key=run_key,
        exit_code=exit_code,
        resolved=len(outcome.resolved_ids),
        unresolved=len(outcome.unresolved_ids),
        errors=len(outcome.error_ids),
    )
    return outcome


#: Where the harness writes per-instance reports, relative to its working
#: directory. swebench >= 3 uses ``logs/run_evaluation``; older releases and
#: some documentation use ``logs/evaluation``. Both are checked so an upgrade
#: or downgrade of the harness does not silently produce "no results".
EVALUATION_LOG_DIRS: tuple[str, ...] = ("logs/run_evaluation", "logs/evaluation")


def parse_evaluation_report(cwd: Path, run_key: str, model_name: str) -> EvaluationOutcome:
    """Read the harness's output files.

    Per-instance ``report.json`` files are authoritative: they are written by
    the harness's own grading code. The batch summary fills in counts and
    catches instances that never produced a per-instance report at all.

    Layouts handled (swebench 5.x verified against the installed package):
      * ``<cwd>/logs/run_evaluation/<run_id>/<model>/<instance>/report.json``
      * ``<cwd>/<model>.<run_id>.json``  (batch summary, ``--report_dir``)
    """
    outcome = EvaluationOutcome(ok=False)

    for relative in EVALUATION_LOG_DIRS:
        evaluation_root = cwd / Path(relative) / run_key
        if not evaluation_root.exists():
            continue
        for report_path in sorted(evaluation_root.glob("**/report.json")):
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("swebench.bad_report", path=str(report_path), error=str(exc))
                continue
            outcome.report_paths.append(str(report_path))
            _absorb_instance_report(outcome, data, report_path)

    for candidate in _summary_candidates(cwd, run_key, model_name):
        if not candidate.exists():
            continue
        try:
            summary = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        outcome.summary = summary
        outcome.report_paths.append(str(candidate))
        _absorb_summary(outcome, summary)
        break

    return outcome


def _summary_candidates(cwd: Path, run_key: str, model_name: str) -> list[Path]:
    """Every place the batch summary has been written, most likely first."""
    safe_model = model_name.replace("/", "__")
    candidates = [
        cwd / f"{safe_model}.{run_key}.json",  # swebench >= 3, --report_dir default
        cwd / f"{model_name}.{run_key}.json",
    ]
    for relative in EVALUATION_LOG_DIRS:
        candidates += [
            cwd / Path(relative) / run_key / "results.json",
            cwd / Path(relative) / run_key / f"{safe_model}.json",
        ]
    return candidates


def _absorb_instance_report(
    outcome: EvaluationOutcome, data: dict[str, Any], report_path: Path
) -> None:
    """A per-instance report is ``{instance_id: {...}}`` or a bare body."""
    entries: dict[str, Any]
    if len(data) == 1 and isinstance(next(iter(data.values())), dict):
        entries = data
    else:
        entries = {report_path.parent.name: data}

    for instance_id, body in entries.items():
        if not isinstance(body, dict):
            continue
        outcome.per_instance[instance_id] = body

        # Schema (swebench.harness.grading.get_eval_report):
        #   patch_is_None, patch_exists, patch_successfully_applied,
        #   resolved, infra_failure
        if body.get("patch_is_None"):
            outcome.empty_patch_ids.add(instance_id)
        elif body.get("infra_failure"):
            outcome.error_ids.add(instance_id)
        elif bool(body.get("resolved")):
            outcome.resolved_ids.add(instance_id)
        else:
            outcome.unresolved_ids.add(instance_id)


#: Summary keys that mean "this instance was not graded successfully".
_SUMMARY_ERROR_KEYS = (
    "error_ids",
    "infra_failure_ids",
    "ambiguous_failure_ids",
    "incomplete_ids",
)


def _absorb_summary(outcome: EvaluationOutcome, summary: dict[str, Any]) -> None:
    """Fill in from the batch summary without overriding per-instance verdicts."""
    buckets: list[tuple[str, set[str]]] = [
        ("resolved_ids", outcome.resolved_ids),
        ("unresolved_ids", outcome.unresolved_ids),
        ("empty_patch_ids", outcome.empty_patch_ids),
        *[(key, outcome.error_ids) for key in _SUMMARY_ERROR_KEYS],
    ]
    for key, target in buckets:
        for instance_id in summary.get(key, []) or []:
            if instance_id not in outcome.per_instance:
                target.add(instance_id)
                outcome.per_instance.setdefault(instance_id, {"source": "summary", "bucket": key})

    # A per-instance report always wins over a summary listing.
    outcome.unresolved_ids -= outcome.resolved_ids
    outcome.error_ids -= outcome.resolved_ids | outcome.unresolved_ids
    outcome.empty_patch_ids -= outcome.resolved_ids | outcome.unresolved_ids
