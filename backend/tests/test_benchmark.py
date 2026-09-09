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
    path.write_text(json.dumps({"resolved_ids": ["a__a-1"], "unresolved_ids": []}), encoding="utf-8")
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
    assert [i.instance_id for i in load_instances(str(path), "test", limit=1, offset=1)] == ["b__b-2"]
    assert [
        i.instance_id for i in load_instances(str(path), "test", instance_ids=["b__b-2"])
    ] == ["b__b-2"]


def test_unknown_instance_id_is_reported(tmp_path: Path) -> None:
    from patchpilot.errors import BenchmarkError

    path = tmp_path / "mini.json"
    path.write_text(
        json.dumps([{"instance_id": "a__a-1", "repo": "o/a", "base_commit": "x",
                     "problem_statement": "p"}]),
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

    assert instance_image_name(
        "Foo__Bar-1", namespace="myorg", tag="v2", arch="x86_64"
    ) == "myorg/sweb.eval.x86_64.foo_1776_bar-1:v2"

    # No namespace means a purely local image: no dunder substitution needed.
    assert instance_image_name(
        "foo__bar-1", namespace=None, arch="x86_64"
    ) == "sweb.eval.x86_64.foo__bar-1:latest"


def test_docker_arch_maps_apple_silicon() -> None:
    from patchpilot.bench.images import docker_arch

    assert docker_arch("arm64") == "arm64"
    assert docker_arch("aarch64") == "arm64"
    assert docker_arch("AMD64") == "x86_64"
    assert docker_arch("x86_64") == "x86_64"
