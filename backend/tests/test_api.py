"""API tests against a real database and a real app instance."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from patchpilot.agent.result import AgentResult, RunMetrics
from patchpilot.api.app import create_app
from patchpilot.db.models import BenchmarkRun, BenchmarkTask, Run
from patchpilot.db.session import dispose_db, init_db, session_scope
from patchpilot.domain.enums import BenchmarkRunStatus, BenchmarkStatus, RunState
from patchpilot.events.bus import EventBus
from patchpilot.events.recorder import DatabaseSink
from patchpilot.ids import new_id, short_code
from patchpilot.runs import service


@pytest.fixture
async def app_settings(settings, tmp_path):
    """Settings with an isolated SQLite file and the embedded worker disabled."""
    settings.database_url = f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}"
    settings.worker_enabled = False
    settings.provider = "scripted"
    await dispose_db()
    await init_db(settings)
    yield settings
    await dispose_db()


@pytest.fixture
async def client(app_settings):
    from patchpilot.config import get_settings

    app = create_app(app_settings)
    app.dependency_overrides[get_settings] = lambda: app_settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


# ------------------------------------------------------------------- system


async def test_health(client) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_status_reports_the_environment_honestly(client) -> None:
    response = await client.get("/api/status")
    assert response.status_code == 200
    body = response.json()

    assert body["provider"]["provider"] == "scripted"
    assert body["provider"]["is_test_double"] is True
    assert any("test-double" in w for w in body["warnings"])
    assert any("Local sandbox" in w for w in body["warnings"])
    assert body["database"] == "sqlite"
    assert len(body["tools"]) >= 14
    assert body["policy"]["default"] == "deny"


async def test_config_never_exposes_a_key(client, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret-value")
    response = await client.get("/api/config")
    body = json.dumps(response.json())
    assert "sk-super-secret-value" not in body


async def test_fixtures_endpoint(client) -> None:
    response = await client.get("/api/runs/fixtures")
    assert response.status_code == 200
    ids = {f["id"] for f in response.json()}
    assert {"calculator_bug", "auth_token_bug", "pagination_bug"} <= ids


# --------------------------------------------------------------------- runs


async def test_create_run_queues_without_blocking(client) -> None:
    response = await client.post(
        "/api/runs",
        json={"issue": "divide() crashes on zero denominators", "fixture": "calculator_bug"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "PENDING"
    assert body["code"] and len(body["code"]) == 4
    assert body["repository"]

    detail = await client.get(f"/api/runs/{body['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == body["id"]


async def test_a_run_is_retrievable_by_its_short_code(client) -> None:
    created = (
        await client.post(
            "/api/runs", json={"issue": "x" * 20, "fixture": "calculator_bug"}
        )
    ).json()
    response = await client.get(f"/api/runs/{created['code']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]


async def test_create_run_requires_a_source(client) -> None:
    response = await client.post("/api/runs", json={"issue": "something is broken here"})
    assert response.status_code == 422


async def test_create_run_rejects_a_short_issue(client) -> None:
    response = await client.post("/api/runs", json={"issue": "bug", "fixture": "calculator_bug"})
    assert response.status_code == 422


async def test_unknown_fixture_is_404(client) -> None:
    response = await client.post(
        "/api/runs", json={"issue": "x" * 20, "fixture": "does_not_exist"}
    )
    assert response.status_code == 404


async def test_missing_local_path_is_400(client) -> None:
    response = await client.post(
        "/api/runs", json={"issue": "x" * 20, "local_path": "/nope/not/here"}
    )
    assert response.status_code == 400


async def test_unknown_run_is_404(client) -> None:
    assert (await client.get("/api/runs/nonexistent")).status_code == 404


async def test_list_runs_paginates(client) -> None:
    for i in range(3):
        await client.post(
            "/api/runs", json={"issue": f"issue number {i} is broken", "fixture": "calculator_bug"}
        )
    response = await client.get("/api/runs?limit=2")
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


async def test_cancel_marks_the_run(client) -> None:
    created = (
        await client.post("/api/runs", json={"issue": "x" * 20, "fixture": "calculator_bug"})
    ).json()
    response = await client.post(f"/api/runs/{created['id']}/cancel")
    assert response.status_code == 202
    detail = (await client.get(f"/api/runs/{created['id']}")).json()
    assert detail["cancel_requested"] is True


# ------------------------------------------------------- persisted run views


async def _seed_finished_run(app_settings) -> str:
    """Create a run, stream real events through the DB sink, and finalise it."""
    async with session_scope(app_settings) as session:
        run = Run(
            id=new_id(),
            code=short_code(),
            repository="demo/project",
            issue="divide() crashes when the denominator is zero",
            state=str(RunState.PENDING),
            provider="scripted",
            model="scripted-model",
        )
        session.add(run)
        await session.flush()
        run_id = run.id

    sink = DatabaseSink(run_id, app_settings)
    bus = EventBus(run_id, [sink])
    from patchpilot.domain.enums import EventType

    await bus.emit(EventType.RUN_STARTED, {"repository": "demo/project"})
    await bus.emit(EventType.STATE_CHANGED, {"state": "SEARCHING"})
    await bus.emit(
        EventType.TOOL_CALLED,
        {"tool": "search_code", "arguments": {"query": "divide"}, "call_id": "c1"},
        iteration=1,
    )
    await bus.emit(
        EventType.TOOL_RESULT,
        {"tool": "search_code", "call_id": "c1", "ok": True, "summary": "3 matches",
         "duration_ms": 12, "data": {}},
        iteration=1,
    )
    await bus.emit(
        EventType.HYPOTHESIS_CREATED,
        {"index": 1, "cause": "no zero guard", "evidence": "calculator.py:24",
         "files": ["src/calculator.py"], "fix": "return None", "validation": "pytest"},
        iteration=1,
    )
    await bus.emit(
        EventType.TEST_FINISHED,
        {"command": "pytest -q", "passed": 10, "failed": 0, "errors": 0, "skipped": 0,
         "total": 10, "green": True, "framework": "pytest", "timed_out": False,
         "failing_tests": [], "duration_ms": 900, "output": "10 passed"},
        iteration=2,
    )

    diff = (
        "diff --git a/src/calculator.py b/src/calculator.py\n"
        "--- a/src/calculator.py\n"
        "+++ b/src/calculator.py\n"
        "@@ -22,3 +22,5 @@ def divide(a, b):\n"
        " \n"
        "+    if b == 0:\n"
        "+        return None\n"
        "     return a / b\n"
    )
    result = AgentResult(run_id=run_id, state=RunState.RESOLVED)
    result.final_patch = diff
    result.summary = "Guarded divide() against a zero denominator."
    result.stop_reason = "tests passed"
    result.files_inspected = ["src/calculator.py", "tests/test_calculator.py"]
    result.files_modified = ["src/calculator.py"]
    result.metrics = RunMetrics(iterations=3, tool_calls=6, test_runs=1, files_modified=1)

    async with session_scope(app_settings) as session:
        await service.apply_result(session, run_id, result)
    return run_id


async def test_events_are_persisted_and_ordered(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    response = await client.get(f"/api/runs/{run_id}/events")
    events = response.json()

    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)
    types = [e["type"] for e in events]
    assert "TOOL_CALLED" in types
    assert "HYPOTHESIS_CREATED" in types
    assert "TEST_FINISHED" in types


async def test_events_support_a_resume_cursor(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    everything = (await client.get(f"/api/runs/{run_id}/events")).json()
    tail = (await client.get(f"/api/runs/{run_id}/events?after=3")).json()
    assert all(e["seq"] > 3 for e in tail)
    assert len(tail) == len(everything) - 3


async def test_tool_calls_are_projected_with_their_arguments(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    rows = (await client.get(f"/api/runs/{run_id}/tools")).json()
    assert len(rows) == 1
    assert rows[0]["tool"] == "search_code"
    # Arguments come from TOOL_CALLED, the outcome from TOOL_RESULT.
    assert rows[0]["arguments"] == {"query": "divide"}
    assert rows[0]["ok"] is True


async def test_test_runs_are_projected(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    rows = (await client.get(f"/api/runs/{run_id}/tests")).json()
    assert len(rows) == 1
    assert rows[0]["passed"] == 10
    assert rows[0]["green"] is True


async def test_hypotheses_are_projected(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    rows = (await client.get(f"/api/runs/{run_id}/hypotheses")).json()
    assert len(rows) == 1
    assert rows[0]["suspected_cause"] == "no zero guard"
    assert rows[0]["files_involved"] == ["src/calculator.py"]


async def test_diff_endpoint_returns_structured_hunks(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    body = (await client.get(f"/api/runs/{run_id}/diff")).json()

    assert body["additions"] == 2
    assert len(body["files"]) == 1
    file = body["files"][0]
    assert file["path"] == "src/calculator.py"
    assert file["hunks"]
    kinds = [line["kind"] for line in file["hunks"][0]["lines"]]
    assert "add" in kinds and "context" in kinds


async def test_files_endpoint_marks_modified_files(client, app_settings) -> None:
    run_id = await _seed_finished_run(app_settings)
    body = (await client.get(f"/api/runs/{run_id}/files")).json()
    modified = {f["path"] for f in body["inspected"] if f["modified"]}
    assert modified == {"src/calculator.py"}
    assert body["inspected_count"] == 2


async def test_stats(client, app_settings) -> None:
    await _seed_finished_run(app_settings)
    body = (await client.get("/api/stats")).json()
    assert body["total"] >= 1
    assert body["resolved"] >= 1


# --------------------------------------------------------------- benchmarks


async def test_benchmark_preflight_reports_requirements(client) -> None:
    body = (await client.get("/api/benchmarks/preflight")).json()
    assert "harness_installed" in body
    assert "docker_available" in body
    assert isinstance(body["ready"], bool)
    if not body["ready"]:
        assert body["problems"]


async def test_leaderboard_is_empty_until_something_is_evaluated(client) -> None:
    body = (await client.get("/api/benchmarks/leaderboard")).json()
    assert body["has_results"] is False
    assert body["best"] is None
    assert "No SWE-bench batch has been graded" in body["note"]


async def _seed_benchmark(app_settings, *, evaluated: bool) -> str:
    async with session_scope(app_settings) as session:
        benchmark = BenchmarkRun(
            id=new_id(),
            name="lite x2",
            dataset="princeton-nlp/SWE-bench_Lite",
            split="test",
            run_key="patchpilot-test01",
            provider="anthropic",
            model="claude-sonnet-5",
            status=str(BenchmarkRunStatus.COMPLETED),
            total_tasks=2,
            completed_tasks=2,
            resolved_tasks=1 if evaluated else 0,
            unresolved_tasks=1 if evaluated else 0,
            evaluated=evaluated,
            instance_ids=["a__a-1", "b__b-2"],
        )
        session.add(benchmark)
        await session.flush()
        for instance_id, status in (
            ("a__a-1", BenchmarkStatus.RESOLVED if evaluated else BenchmarkStatus.NOT_EVALUATED),
            ("b__b-2", BenchmarkStatus.UNRESOLVED if evaluated else BenchmarkStatus.NOT_EVALUATED),
        ):
            session.add(
                BenchmarkTask(
                    id=new_id(),
                    benchmark_id=benchmark.id,
                    instance_id=instance_id,
                    repo="o/r",
                    base_commit="abc",
                    problem_statement="p",
                    agent_status=str(RunState.RESOLVED),
                    benchmark_status=str(status),
                    model_patch="diff --git a/x b/x\n",
                )
            )
        return benchmark.id


async def test_resolve_rate_is_null_until_officially_evaluated(client, app_settings) -> None:
    """An ungraded batch must never render as 0%."""
    benchmark_id = await _seed_benchmark(app_settings, evaluated=False)
    body = (await client.get(f"/api/benchmarks/{benchmark_id}")).json()
    assert body["evaluated"] is False
    assert body["resolve_rate"] is None


async def test_resolve_rate_comes_from_the_evaluator(client, app_settings) -> None:
    benchmark_id = await _seed_benchmark(app_settings, evaluated=True)
    body = (await client.get(f"/api/benchmarks/{benchmark_id}")).json()
    assert body["evaluated"] is True
    assert body["resolve_rate"] == 50.0
    assert body["resolved_tasks"] == 1


async def test_leaderboard_uses_only_evaluated_batches(client, app_settings) -> None:
    await _seed_benchmark(app_settings, evaluated=False)
    assert (await client.get("/api/benchmarks/leaderboard")).json()["has_results"] is False

    await _seed_benchmark(app_settings, evaluated=True)
    body = (await client.get("/api/benchmarks/leaderboard")).json()
    assert body["has_results"] is True
    assert body["best"]["resolve_rate"] == 50.0
    assert "50.0%" in body["resume_bullet"]
    assert "official SWE-bench harness" in body["resume_bullet"]


async def test_benchmark_tasks_and_predictions(client, app_settings) -> None:
    benchmark_id = await _seed_benchmark(app_settings, evaluated=True)

    tasks = (await client.get(f"/api/benchmarks/{benchmark_id}/tasks")).json()
    assert len(tasks) == 2
    assert {t["benchmark_status"] for t in tasks} == {"RESOLVED", "UNRESOLVED"}

    predictions = (await client.get(f"/api/benchmarks/{benchmark_id}/predictions")).json()
    assert len(predictions) == 2
    for row in predictions:
        assert set(row) == {"instance_id", "model_name_or_path", "model_patch"}


async def test_benchmark_task_detail(client, app_settings) -> None:
    benchmark_id = await _seed_benchmark(app_settings, evaluated=True)
    body = (await client.get(f"/api/benchmarks/{benchmark_id}/tasks/a__a-1")).json()
    assert body["task"]["instance_id"] == "a__a-1"
    assert body["task"]["benchmark_status"] == "RESOLVED"
    assert "diff" in body


async def test_creating_a_benchmark_requires_a_selection(client) -> None:
    response = await client.post("/api/benchmarks", json={})
    assert response.status_code == 422


async def test_creating_a_benchmark_from_a_local_dataset(client, app_settings, tmp_path) -> None:
    dataset = tmp_path / "mini.jsonl"
    dataset.write_text(
        "\n".join(
            json.dumps(
                {
                    "instance_id": f"o__r-{i}",
                    "repo": "o/r",
                    "base_commit": "abc",
                    "problem_statement": "a bug",
                    "FAIL_TO_PASS": ["t::x"],
                    "PASS_TO_PASS": [],
                }
            )
            for i in range(3)
        ),
        encoding="utf-8",
    )
    response = await client.post(
        "/api/benchmarks",
        json={"dataset": str(dataset), "limit": 2, "evaluate": False, "name": "local mini"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["total_tasks"] == 2
    assert body["status"] == "PENDING"
    assert body["evaluated"] is False
    assert body["resolve_rate"] is None
    assert body["run_key"].startswith("patchpilot-")


# --------------------------------------------------------------------- mcp


async def test_mcp_status(client) -> None:
    body = (await client.get("/api/mcp/status")).json()
    assert "enabled" in body
    assert "servers" in body
    assert body["config_path"]


async def test_mcp_tools_shows_the_policy_verdict(client) -> None:
    body = (await client.get("/api/mcp/tools")).json()
    assert body["allowed_count"] >= 14
    names = {t["name"] for t in body["tools"]}
    assert "read_file" in names
    assert all(t["origin"] == "native" for t in body["tools"])


# ------------------------------------------------------ timestamp round-trip


async def test_timestamps_come_back_timezone_aware(app_settings) -> None:
    """SQLite drops tzinfo, and the first subtraction then throws.

    A 15-instance benchmark batch died with "can't subtract offset-naive and
    offset-aware datetimes" before running a single task, because a timestamp
    written as aware was read back naive and compared against a fresh
    `datetime.now(UTC)`.
    """
    from datetime import UTC, datetime, timedelta

    async with session_scope(app_settings) as session:
        run = Run(
            id=new_id(),
            code=short_code(),
            repository="demo/tz",
            issue="x" * 20,
            state=str(RunState.PENDING),
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        run_id = run.id

    await dispose_db()
    await init_db(app_settings)

    async with session_scope(app_settings) as session:
        reloaded = await session.get(Run, run_id)
        assert reloaded is not None
        assert reloaded.queued_at.tzinfo is not None, "queued_at lost its timezone"
        assert reloaded.started_at is not None
        assert reloaded.started_at.tzinfo is not None, "started_at lost its timezone"

        # The operation that actually failed in production.
        elapsed = datetime.now(UTC) - reloaded.started_at
        assert isinstance(elapsed, timedelta)
        assert elapsed.total_seconds() >= 0


async def test_benchmark_aggregate_survives_reloaded_timestamps(app_settings) -> None:
    """The exact call path that killed the batch: _aggregate over stored rows."""
    from datetime import UTC, datetime, timedelta

    from patchpilot.bench.runner import _recompute

    async with session_scope(app_settings) as session:
        benchmark = BenchmarkRun(
            id=new_id(), name="tz", run_key="patchpilot-tz", model="m", total_tasks=1
        )
        session.add(benchmark)
        await session.flush()
        session.add(
            BenchmarkTask(
                id=new_id(),
                benchmark_id=benchmark.id,
                instance_id="a__a-1",
                repo="o/r",
                base_commit="abc",
                problem_statement="p",
                started_at=datetime.now(UTC) - timedelta(seconds=30),
                finished_at=datetime.now(UTC),
                metrics={"iterations": 3},
            )
        )
        benchmark_id = benchmark.id

    await dispose_db()
    await init_db(app_settings)

    async with session_scope(app_settings) as session:
        await _recompute(session, benchmark_id)
        refreshed = await session.get(BenchmarkRun, benchmark_id)
        assert refreshed is not None
        assert refreshed.aggregate["avg_runtime_s"] is not None
        assert refreshed.aggregate["avg_runtime_s"] > 0
