"""SWE-bench integration tests.

The evaluator-report parser is tested against the *exact* file layout and JSON
schema that ``swebench`` 5.x writes (verified by reading the installed
package's ``reporting.make_run_report`` and ``grading.get_eval_report``).
Getting this wrong would silently report 0% forever, so it is pinned here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from patchpilot.bench.dataset import SweBenchInstance, load_instances
from patchpilot.bench.evaluator import (
    Prediction,
    parse_evaluation_report,
    write_predictions,
)
from patchpilot.bench.runner import build_agent_task
from patchpilot.domain.enums import BenchmarkStatus

MODEL = "claude-sonnet-5"
RUN_KEY = "patchpilot-abc123"


def _write_instance_report(
    cwd: Path, instance_id: str, body: dict, *, log_dir: str = "logs/run_evaluation"
) -> None:
    path = cwd / log_dir / RUN_KEY / MODEL.replace("/", "__") / instance_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.json").write_text(json.dumps({instance_id: body}), encoding="utf-8")


def _resolved_body() -> dict:
    return {
        "patch_is_None": False,
        "patch_exists": True,
        "patch_successfully_applied": True,
        "resolved": True,
        "infra_failure": False,
    }


def _unresolved_body() -> dict:
    return {**_resolved_body(), "resolved": False}


# --------------------------------------------------------------- predictions


def test_predictions_file_matches_the_harness_schema(tmp_path: Path) -> None:
    path = write_predictions(
        tmp_path / "predictions.jsonl",
        [
            Prediction("astropy__astropy-12907", MODEL, "diff --git a/x b/x\n"),
            Prediction("django__django-11099", MODEL, ""),
        ],
    )
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    for row in lines:
        # The harness requires exactly these three keys.
        assert set(row) == {"instance_id", "model_name_or_path", "model_patch"}
        assert isinstance(row["model_patch"], str)
    assert lines[0]["instance_id"] == "astropy__astropy-12907"


# ------------------------------------------------------------ report parsing


def test_parses_per_instance_reports(tmp_path: Path) -> None:
    _write_instance_report(tmp_path, "astropy__astropy-12907", _resolved_body())
    _write_instance_report(tmp_path, "django__django-11099", _unresolved_body())

    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)

    assert outcome.resolved_ids == {"astropy__astropy-12907"}
    assert outcome.unresolved_ids == {"django__django-11099"}
    assert outcome.status_for("astropy__astropy-12907") is BenchmarkStatus.RESOLVED
    assert outcome.status_for("django__django-11099") is BenchmarkStatus.UNRESOLVED


def test_parses_the_legacy_log_directory(tmp_path: Path) -> None:
    """Older harness releases wrote to logs/evaluation instead."""
    _write_instance_report(
        tmp_path, "astropy__astropy-12907", _resolved_body(), log_dir="logs/evaluation"
    )
    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)
    assert outcome.resolved_ids == {"astropy__astropy-12907"}


def test_empty_patch_and_infra_failure_are_not_unresolved(tmp_path: Path) -> None:
    """An ungraded instance must not be silently counted as a failed attempt."""
    _write_instance_report(
        tmp_path, "a__a-1", {"patch_is_None": True, "patch_exists": False, "resolved": False}
    )
    _write_instance_report(
        tmp_path,
        "b__b-2",
        {"patch_is_None": False, "patch_exists": True, "resolved": False, "infra_failure": True},
    )
    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)

    assert outcome.status_for("a__a-1") is BenchmarkStatus.EMPTY_PATCH
    assert outcome.status_for("b__b-2") is BenchmarkStatus.ERROR
    assert outcome.unresolved_ids == set()


def test_parses_the_batch_summary(tmp_path: Path) -> None:
    """Summary filename is '<model with / as __>.<run_id>.json' in report_dir."""
    summary = {
        "total_instances": 4,
        "submitted_instances": 4,
        "completed_instances": 3,
        "resolved_instances": 1,
        "unresolved_instances": 2,
        "resolved_ids": ["a__a-1"],
        "unresolved_ids": ["b__b-2", "c__c-3"],
        "empty_patch_ids": ["d__d-4"],
        "error_ids": [],
        "infra_failure_ids": [],
        "ambiguous_failure_ids": [],
        "incomplete_ids": [],
        "schema_version": 2,
    }
    (tmp_path / f"{MODEL}.{RUN_KEY}.json").write_text(json.dumps(summary), encoding="utf-8")

    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)

    assert outcome.resolved_ids == {"a__a-1"}
    assert outcome.unresolved_ids == {"b__b-2", "c__c-3"}
    assert outcome.empty_patch_ids == {"d__d-4"}
    assert outcome.summary["resolved_instances"] == 1


def test_summary_maps_failure_buckets_to_error(tmp_path: Path) -> None:
    summary = {
        "resolved_ids": [],
        "unresolved_ids": [],
        "infra_failure_ids": ["a__a-1"],
        "ambiguous_failure_ids": ["b__b-2"],
        "incomplete_ids": ["c__c-3"],
    }
    (tmp_path / f"{MODEL}.{RUN_KEY}.json").write_text(json.dumps(summary), encoding="utf-8")
    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)
    assert outcome.error_ids == {"a__a-1", "b__b-2", "c__c-3"}
    for instance_id in outcome.error_ids:
        assert outcome.status_for(instance_id) is BenchmarkStatus.ERROR


def test_per_instance_report_beats_the_summary(tmp_path: Path) -> None:
    """The grading code's own output is authoritative."""
    _write_instance_report(tmp_path, "a__a-1", _resolved_body())
    (tmp_path / f"{MODEL}.{RUN_KEY}.json").write_text(
        json.dumps({"resolved_ids": [], "unresolved_ids": ["a__a-1"]}), encoding="utf-8"
    )
    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)
    assert outcome.resolved_ids == {"a__a-1"}
    assert outcome.unresolved_ids == set()


def test_missing_report_yields_no_verdicts(tmp_path: Path) -> None:
    outcome = parse_evaluation_report(tmp_path, RUN_KEY, MODEL)
    assert not outcome.per_instance
    assert outcome.status_for("anything") is BenchmarkStatus.NOT_EVALUATED


def test_slashes_in_model_names_are_handled(tmp_path: Path) -> None:
    model = "org/model-1"
    path = tmp_path / f"{model.replace('/', '__')}.{RUN_KEY}.json"
    path.write_text(
        json.dumps({"resolved_ids": ["a__a-1"], "unresolved_ids": []}), encoding="utf-8"
    )
    outcome = parse_evaluation_report(tmp_path, RUN_KEY, model)
    assert outcome.resolved_ids == {"a__a-1"}


# ----------------------------------------------------------------- dataset


def _fake_instance(**overrides) -> SweBenchInstance:
    data = {
        "instance_id": "django__django-11099",
        "repo": "django/django",
        "base_commit": "d26b2424437dabeeca94d7900b37d2df4410da0c",
        "problem_statement": "UsernameValidator allows trailing newline",
        "FAIL_TO_PASS": '["tests/auth_tests/test_validators.py::UsernameValidatorsTests::test_ascii_validator_with_newline"]',
        "PASS_TO_PASS": '["tests/auth_tests/test_validators.py::UsernameValidatorsTests::test_ascii_validator"]',
    }
    data.update(overrides)
    return SweBenchInstance.from_row(data)


def test_json_encoded_test_lists_are_decoded() -> None:
    """FAIL_TO_PASS arrives as a JSON string in some dataset releases."""
    instance = _fake_instance()
    assert instance.fail_to_pass == [
        "tests/auth_tests/test_validators.py::UsernameValidatorsTests::test_ascii_validator_with_newline"
    ]
    assert len(instance.pass_to_pass) == 1


def test_plain_lists_also_work() -> None:
    instance = _fake_instance(FAIL_TO_PASS=["a::b"], PASS_TO_PASS=["c::d"])
    assert instance.fail_to_pass == ["a::b"]


def test_repo_url_and_test_command() -> None:
    instance = _fake_instance()
    assert instance.repo_url == "https://github.com/django/django.git"
    assert instance.test_files == ["tests/auth_tests/test_validators.py"]
    assert "tests/auth_tests/test_validators.py" in instance.suggested_test_command()


def test_local_dataset_file(tmp_path: Path) -> None:
    path = tmp_path / "mini.jsonl"
    rows = [
        {
            "instance_id": "b__b-2",
            "repo": "o/b",
            "base_commit": "def",
            "problem_statement": "bug b",
            "FAIL_TO_PASS": ["t::b"],
            "PASS_TO_PASS": [],
        },
        {
            "instance_id": "a__a-1",
            "repo": "o/a",
            "base_commit": "abc",
            "problem_statement": "bug a",
            "FAIL_TO_PASS": ["t::a"],
            "PASS_TO_PASS": [],
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    instances = load_instances(str(path), "test")
    # Deterministic ordering matters: a benchmark must be reproducible from
    # its stored (offset, limit).
    assert [i.instance_id for i in instances] == ["a__a-1", "b__b-2"]

    assert [i.instance_id for i in load_instances(str(path), "test", limit=1)] == ["a__a-1"]
    assert [i.instance_id for i in load_instances(str(path), "test", limit=1, offset=1)] == [
        "b__b-2"
    ]
    assert [i.instance_id for i in load_instances(str(path), "test", instance_ids=["b__b-2"])] == [
        "b__b-2"
    ]


def test_unknown_instance_id_is_reported(tmp_path: Path) -> None:
    from patchpilot.errors import BenchmarkError

    path = tmp_path / "mini.json"
    path.write_text(
        json.dumps(
            [{"instance_id": "a__a-1", "repo": "o/a", "base_commit": "x", "problem_statement": "p"}]
        ),
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="not found"):
        load_instances(str(path), "test", instance_ids=["nope"])


# --------------------------------------------------------- no answer leakage


def test_the_agent_is_never_given_the_answer_key() -> None:
    """FAIL_TO_PASS, PASS_TO_PASS and the gold patch must not reach the agent.

    If any of them leaked into the prompt, a resulting resolve rate would be
    meaningless.
    """
    instance = _fake_instance(patch="diff --git a/GOLD b/GOLD\n+the answer\n")
    task = build_agent_task(instance, "run-1")

    haystack = " ".join(
        str(value)
        for value in (task.issue, task.hints, task.extra_guidance, task.repository)
        if value
    )
    assert "GOLD" not in haystack
    assert "the answer" not in haystack
    for node_id in instance.fail_to_pass + instance.pass_to_pass:
        assert node_id not in haystack

    assert task.base_commit == instance.base_commit
    assert task.repo_url == instance.repo_url
    assert task.issue == instance.problem_statement


def test_suggested_test_command_names_files_not_test_ids() -> None:
    """Naming the file is normal engineering; naming the node ids is the key."""
    instance = _fake_instance()
    command = instance.suggested_test_command()
    assert "test_ascii_validator_with_newline" not in command
    assert "tests/auth_tests/test_validators.py" in command


# ----------------------------------------------------- per-instance images


def test_instance_image_matches_the_official_naming() -> None:
    """Verified against swebench.image_builder.image_spec.ImageSpec.name.

    Docker Hub rejects dunders, so the harness substitutes `_1776_`; getting
    this wrong means every pull 404s.
    """
    from patchpilot.bench.images import instance_image_name

    assert (
        instance_image_name("astropy__astropy-12907", arch="x86_64")
        == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest"
    )
    assert (
        instance_image_name("django__django-11099", arch="arm64")
        == "swebench/sweb.eval.arm64.django_1776_django-11099:latest"
    )


def test_instance_image_is_lowercased_and_configurable() -> None:
    from patchpilot.bench.images import instance_image_name

    assert (
        instance_image_name("Foo__Bar-1", namespace="myorg", tag="v2", arch="x86_64")
        == "myorg/sweb.eval.x86_64.foo_1776_bar-1:v2"
    )

    # No namespace means a purely local image: no dunder substitution needed.
    assert (
        instance_image_name("foo__bar-1", namespace=None, arch="x86_64")
        == "sweb.eval.x86_64.foo__bar-1:latest"
    )


def test_docker_arch_maps_apple_silicon() -> None:
    from patchpilot.bench.images import docker_arch

    assert docker_arch("arm64") == "arm64"
    assert docker_arch("aarch64") == "arm64"
    assert docker_arch("AMD64") == "x86_64"
    assert docker_arch("x86_64") == "x86_64"


def test_disk_requirement_scales_with_batch_size() -> None:
    """The ~120 GB figure is for the full 300-instance run, not for any batch.

    A blanket 50 GB gate refused to evaluate a 15-instance same-repo batch that
    needed about 6 GB of images — 75 minutes of generation produced patches
    that were then never graded.
    """
    from patchpilot.bench.evaluator import required_disk_gb

    assert required_disk_gb(1) == 6.0  # floor: working space for one image
    assert required_disk_gb(15) == 14.0
    assert required_disk_gb(300) == 185.0
    # Unknown batch size stays conservative.
    assert required_disk_gb(None) == 50.0
    # Images generation already pulled are not charged again: evaluation reuses
    # them, so a re-grade of an existing batch only needs working space.
    assert required_disk_gb(15, missing_images=0) == 6.0
    assert required_disk_gb(15, missing_images=15) == 14.0
    assert required_disk_gb(15, missing_images=5) < required_disk_gb(15, missing_images=15)
    # Monotonic: a bigger batch never needs less.
    sizes = [1, 5, 15, 50, 100, 300]
    reqs = [required_disk_gb(n) for n in sizes]
    assert reqs == sorted(reqs)


async def test_preflight_passes_for_a_small_batch_on_a_modest_disk(settings) -> None:
    from unittest.mock import patch

    from patchpilot.bench.evaluator import evaluator_preflight

    with (
        patch("patchpilot.bench.evaluator._free_disk_gb", return_value=20.0),
        patch("patchpilot.bench.evaluator._harness_installed", return_value=True),
        patch(
            "patchpilot.bench.evaluator.docker_available",
            return_value=(True, "28.0 (linux)"),
        ),
    ):
        small = await evaluator_preflight(settings, instance_count=15)
        assert small["ready"] is True, small["problems"]
        assert small["notes"], "a modest disk should still be mentioned"

        full = await evaluator_preflight(settings, instance_count=300)
        assert full["ready"] is False
        assert "185 GB" in full["problems"][0]


# --- harness line endings ----------------------------------------------------
# The harness copies the eval script it writes straight into a Linux container.
# On Windows `Path.write_text` translates every \n to \r\n, bash then reads the
# \r as part of each command, dependencies are never installed, gold tests are
# never restored, and every instance grades UNRESOLVED with no test having run.
# The harness still reports a *complete* run, so the only symptom is a silent
# 0%. These tests pin the shim that prevents it.


def test_the_shim_writes_shell_scripts_with_lf(tmp_path: Path) -> None:
    from patchpilot.bench.harness_shim import force_lf_write_text

    force_lf_write_text()
    script = tmp_path / "eval.sh"
    script.write_text("#!/bin/bash\ngit checkout abc123 tests/test_x.py\n")

    raw = script.read_bytes()
    assert b"\r\n" not in raw, "a CR here becomes part of the command inside bash"
    assert raw.endswith(b"tests/test_x.py\n")


def test_the_shim_is_idempotent_and_honours_an_explicit_newline(tmp_path: Path) -> None:
    from patchpilot.bench.harness_shim import force_lf_write_text

    force_lf_write_text()
    force_lf_write_text()  # double application must not nest wrappers

    target = tmp_path / "explicit.txt"
    target.write_text("a\r\nb\r\n", newline="")
    assert target.read_bytes() == b"a\r\nb\r\n"

    with pytest.raises(TypeError):
        target.write_text(b"bytes")  # type: ignore[arg-type]


def test_the_evaluator_launches_the_harness_through_the_shim() -> None:
    from patchpilot.bench.evaluator import HARNESS_SHIM

    assert HARNESS_SHIM.is_file()
    source = (Path(__file__).parent.parent / "patchpilot" / "bench" / "evaluator.py").read_text(
        encoding="utf-8"
    )
    assert '"-m",\n        HARNESS_MODULE,' not in source, (
        "invoking the harness directly reintroduces the CRLF eval.sh bug"
    )
    assert "str(HARNESS_SHIM)," in source


def test_the_shim_does_not_import_the_application() -> None:
    # It runs under settings.swebench_python, which may lack patchpilot's deps.
    source = (Path(__file__).parent.parent / "patchpilot" / "bench" / "harness_shim.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("import patchpilot", "from patchpilot", "from ..", "from ."):
        assert forbidden not in source, f"shim must not {forbidden!r}"


# --- how the headline percentage is computed ---------------------------------


def _rate(resolved: int, unresolved: int, empty: int = 0, error: int = 0) -> float | None:
    from patchpilot.db.models import BenchmarkRun

    run = BenchmarkRun(
        resolved_tasks=resolved,
        unresolved_tasks=unresolved,
        empty_patch_tasks=empty,
        error_tasks=error,
        evaluated=True,
    )
    return run.resolve_rate


def test_a_task_with_no_patch_counts_against_the_resolve_rate() -> None:
    """SWE-bench scores resolved/attempted, not resolved/graded.

    Giving up on an instance produces an empty patch, which the harness reports
    separately from UNRESOLVED. Excluding those from the denominator would mean
    an agent that quit on 6 of 15 tasks scored *higher* (4/9 = 44.4%) than one
    that attempted all 15 with the same 4 successes (4/15 = 26.7%).
    """
    assert _rate(resolved=4, unresolved=5, empty=6) == 26.7
    assert _rate(resolved=4, unresolved=5) == 44.4  # same patches, nothing skipped
    # Harness infrastructure errors are failures of the batch too.
    assert _rate(resolved=1, unresolved=1, error=2) == 25.0


def test_an_ungraded_batch_has_no_rate_at_all() -> None:
    from patchpilot.db.models import BenchmarkRun

    run = BenchmarkRun(
        resolved_tasks=0, unresolved_tasks=0, empty_patch_tasks=0, error_tasks=0, evaluated=False
    )
    assert run.resolve_rate is None, "an ungraded batch must not render as 0%"
