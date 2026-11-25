"""End-to-end tests: HTTP API → job queue → worker → agent → events → API.

These are the "definition of done" checks. Everything is real — a real FastAPI
app, a real SQLite database, the real worker, the real agent loop, a real git
workspace and real pytest subprocesses. Only the *model* is a scripted double,
so the tests are deterministic and free while still exercising every seam.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from patchpilot.api.app import create_app
from patchpilot.config import get_settings
from patchpilot.db.session import dispose_db, init_db
from patchpilot.providers.scripted import ScriptedTurn, last_tool_output
from patchpilot.worker.runner import execute_run

pytestmark = pytest.mark.integration


@pytest.fixture
async def e2e(settings, tmp_path, monkeypatch):
    """A live app plus a worker we drive explicitly, sharing one database."""
    settings.database_url = f"sqlite+aiosqlite:///{(tmp_path / 'e2e.db').as_posix()}"
    settings.workspace_root = tmp_path / "ws"
    settings.worker_enabled = False  # the tests drive execute_run themselves
    settings.provider = "scripted"
    settings.max_iterations = 12

    await dispose_db()
    await init_db(settings)

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, settings
    await dispose_db()


def install_provider(monkeypatch, responder) -> None:
    """Make the worker's provider factory return our scripted double."""
    from patchpilot.agent import session as session_module
    from patchpilot.providers.scripted import ScriptedProvider

    monkeypatch.setattr(
        session_module,
        "create_provider",
        lambda *args, **kwargs: ScriptedProvider(responder=responder),
    )


def calculator_responder(messages, index):
    """Investigate → hypothesise → patch → test (red) → repatch → test (green)."""
    if index == 0:
        return ScriptedTurn(
            text="Locating the divide implementation.",
            tool_calls=[("search_code", {"query": "def divide"})],
        )
    if index == 1:
        return ScriptedTurn(
            text="Reading the module.",
            tool_calls=[("read_file", {"path": "src/calculator.py"})],
        )
    if index == 2:
        return ScriptedTurn(
            text="Recording a hypothesis.",
            tool_calls=[
                (
                    "record_hypothesis",
                    {
                        "suspected_cause": "divide() divides without guarding b == 0",
                        "evidence": "src/calculator.py returns a / b directly",
                        "files_involved": ["src/calculator.py"],
                        "proposed_fix": "return None when b == 0",
                        "validation_plan": "python -m pytest tests/ -q",
                    },
                )
            ],
        )
    if index == 3:
        # Deliberately wrong first attempt: guards only positive denominators.
        return ScriptedTurn(
            text="Adding a guard.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    return a / b",
                        "new_string": "    if b == 0 and a > 0:\n        return None\n    return a / b",
                    },
                )
            ],
        )
    if index == 4:
        return ScriptedTurn(text="Running tests.", tool_calls=[("run_tests", {})])
    if index == 5:
        observation = last_tool_output(messages, "run_tests")
        assert "failed" in observation, observation[:300]
        return ScriptedTurn(
            text="The guard is too narrow; removing the a > 0 condition.",
            tool_calls=[
                (
                    "edit_file",
                    {
                        "path": "src/calculator.py",
                        "old_string": "    if b == 0 and a > 0:",
                        "new_string": "    if b == 0:",
                    },
                )
            ],
        )
    if index == 6:
        return ScriptedTurn(text="Re-running tests.", tool_calls=[("run_tests", {})])
    return ScriptedTurn(
        text="Green.",
        tool_calls=[
            (
                "finish",
                {
                    "resolved": True,
                    "summary": "divide() now returns None for a zero denominator.",
                },
            )
        ],
    )


async def test_full_run_through_the_http_api(e2e, monkeypatch) -> None:
    """Definition of done, items 2-11, end to end."""
    client, settings = e2e
    install_provider(monkeypatch, calculator_responder)

    # 2. Submit a repository + bug description.
    created = await client.post(
        "/api/runs",
        json={
            "issue": "divide() raises ZeroDivisionError when the denominator is zero; "
            "it should return None as its docstring promises.",
            "fixture": "calculator_bug",
        },
    )
    assert created.status_code == 201
    run_id = created.json()["id"]
    assert created.json()["state"] == "PENDING"

    # The worker picks the job up off the queue and runs it.
    await execute_run(run_id, settings)

    # 3-5. The run investigated, patched and finished.
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["state"] == "RESOLVED", detail["stop_reason"]
    assert detail["has_patch"] is True
    assert "src/calculator.py" in detail["files_modified"]

    # 4. Which tools and files it used.
    calls = (await client.get(f"/api/runs/{run_id}/tools")).json()
    used = [c["tool"] for c in calls]
    assert "search_code" in used
    assert "read_file" in used
    assert "record_hypothesis" in used
    assert "edit_file" in used
    assert used.count("run_tests") == 2

    files = (await client.get(f"/api/runs/{run_id}/files")).json()
    assert files["modified"] == ["src/calculator.py"]
    assert files["inspected_count"] >= 1

    # 6-9. A patch, tests executing, a red result, a second attempt, then green.
    tests = (await client.get(f"/api/runs/{run_id}/tests")).json()
    assert len(tests) == 2
    assert tests[0]["green"] is False and tests[0]["failed"] > 0
    assert tests[1]["green"] is True and tests[1]["failed"] == 0
    assert tests[1]["passed"] == 10

    # 9. The final diff.
    diff = (await client.get(f"/api/runs/{run_id}/diff")).json()
    assert diff["files"][0]["path"] == "src/calculator.py"
    assert diff["additions"] >= 2
    added = [
        line["content"]
        for f in diff["files"]
        for h in f["hunks"]
        for line in h["lines"]
        if line["kind"] == "add"
    ]
    assert any("if b == 0:" in line for line in added)
    assert any("return None" in line for line in added)

    # 11. A complete, ordered, structured run history.
    events = (await client.get(f"/api/runs/{run_id}/events")).json()
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    types = [e["type"] for e in events]
    for expected in (
        "REPOSITORY_READY",
        "ISSUE_ANALYZED",
        "TOOL_CALLED",
        "TOOL_RESULT",
        "HYPOTHESIS_CREATED",
        "TEST_STARTED",
        "TEST_FINISHED",
        "RUN_RESOLVED",
    ):
        assert expected in types, f"missing {expected}"

    # No hidden reasoning is ever stored.
    assert "THINKING" not in types
    assert "REASONING" not in types

    hypotheses = (await client.get(f"/api/runs/{run_id}/hypotheses")).json()
    assert len(hypotheses) == 1
    assert "divide()" in hypotheses[0]["suspected_cause"]
    assert hypotheses[0]["files_involved"] == ["src/calculator.py"]
    assert hypotheses[0]["validation_plan"]

    metrics = detail["metrics"]
    assert metrics["test_runs"] == 2
    assert metrics["files_modified"] == 1
    assert metrics["iterations"] >= 7


async def test_events_can_be_resumed_from_a_cursor(e2e, monkeypatch) -> None:
    """A reconnecting client resumes exactly where it left off."""
    client, settings = e2e
    install_provider(monkeypatch, calculator_responder)

    run_id = (
        await client.post(
            "/api/runs",
            json={"issue": "divide crashes on zero" * 3, "fixture": "calculator_bug"},
        )
    ).json()["id"]
    await execute_run(run_id, settings)

    everything = (await client.get(f"/api/runs/{run_id}/events")).json()
    assert len(everything) > 10

    midpoint = everything[len(everything) // 2]["seq"]
    tail = (await client.get(f"/api/runs/{run_id}/events?after={midpoint}")).json()
    assert all(e["seq"] > midpoint for e in tail)
    assert [e["seq"] for e in tail] == [e["seq"] for e in everything if e["seq"] > midpoint]


async def test_a_failing_run_is_recorded_honestly(e2e, monkeypatch) -> None:
    """When the agent cannot fix it, that is what the record says."""
    client, settings = e2e

    def responder(messages, index):
        if index == 0:
            return ScriptedTurn(tool_calls=[("run_tests", {})])
        return ScriptedTurn(
            tool_calls=[
                (
                    "finish",
                    {
                        "resolved": False,
                        "summary": "Could not identify the root cause.",
                        "failure_class": "WRONG_ASSUMPTION",
                    },
                )
            ]
        )

    install_provider(monkeypatch, responder)
    run_id = (
        await client.post(
            "/api/runs",
            json={"issue": "something is wrong somewhere" * 2, "fixture": "pagination_bug"},
        )
    ).json()["id"]
    await execute_run(run_id, settings)

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["state"] == "FAILED"
    assert detail["failure_class"] == "WRONG_ASSUMPTION"
    assert detail["has_patch"] is False
    assert "Could not identify" in detail["summary"]


async def test_a_revised_run_records_its_second_execution(e2e, monkeypatch) -> None:
    """A revision re-executes the same run id; its events must not be lost.

    Event numbers are unique per run. A second execution that numbered its
    events from 1 again collided with the first execution's, every insert
    failed, and because the bus deliberately swallows persistence errors the
    revision ran with no timeline, no tool calls and no test runs on record.
    """
    client, settings = e2e
    install_provider(monkeypatch, calculator_responder)
    run_id = (
        await client.post(
            "/api/runs",
            json={"issue": "divide crashes on zero" * 3, "fixture": "calculator_bug"},
        )
    ).json()["id"]
    await execute_run(run_id, settings)
    first = (await client.get(f"/api/runs/{run_id}/events")).json()
    assert (await client.get(f"/api/runs/{run_id}")).json()["state"] == "RESOLVED"

    # A cancel aimed at the first execution must not stop the revision.
    from patchpilot.db.models import Run
    from patchpilot.db.session import session_scope

    async with session_scope(settings) as session:
        (await session.get(Run, run_id)).cancel_requested = True

    revised = await client.post(
        f"/api/runs/{run_id}/review/revision",
        json={"feedback": "Please also cover negative zero.", "reviewer": "alice"},
    )
    assert revised.status_code == 200, revised.text

    def revision(messages, index):
        if index == 0:
            return ScriptedTurn(tool_calls=[("run_tests", {})])
        return ScriptedTurn(
            tool_calls=[("finish", {"resolved": True, "summary": "Reviewed patch re-verified."})]
        )

    install_provider(monkeypatch, revision)
    await execute_run(run_id, settings)

    events = (await client.get(f"/api/runs/{run_id}/events")).json()
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    assert len(events) > len(first), "the revision's events were not recorded"
    types = [e["type"] for e in events]
    assert types.count("REPOSITORY_READY") == 2, "the second execution left no trace"

    tools = [c["tool"] for c in (await client.get(f"/api/runs/{run_id}/tools")).json()]
    assert tools.count("run_tests") == 3, tools
    tests = (await client.get(f"/api/runs/{run_id}/tests")).json()
    assert len(tests) == 3 and tests[-1]["green"] is True

    # The restored patch is the revision's change, so re-testing it green is
    # verification, not "success without an observed green test run".
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["state"] == "RESOLVED"
    assert detail["metrics"]["verified_by_tests"] is True
    assert "without an observed green" not in str(
        [e["payload"] for e in events if e["type"] == "WARNING"]
    )


async def test_a_docker_run_never_falls_back_to_the_host(e2e, monkeypatch) -> None:
    """Asking for container isolation and not getting it is a failed run.

    The worker used to degrade to the local sandbox when Docker was down,
    leaving `sandbox: docker` on the run record while repository code ran on
    the host as the user. The reason has to reach the user instead.
    """
    from patchpilot.errors import SandboxError
    from patchpilot.sandbox.docker import DockerSandbox

    async def daemon_down(self) -> None:
        raise SandboxError(
            "Docker sandbox requested but the daemon is unavailable: test. "
            "Start Docker, or set PATCHPILOT_SANDBOX=local (weaker isolation)."
        )

    monkeypatch.setattr(DockerSandbox, "start", daemon_down)
    client, settings = e2e

    def must_not_run(messages, index):
        raise AssertionError("the agent must not run without its sandbox")

    install_provider(monkeypatch, must_not_run)
    run_id = (
        await client.post(
            "/api/runs",
            json={"issue": "divide crashes on zero" * 3, "fixture": "calculator_bug",
                  "sandbox": "docker"},
        )
    ).json()["id"]
    await execute_run(run_id, settings)

    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["state"] == "FAILED"
    assert detail["stop_reason"].startswith("run could not start:")
    assert "daemon is unavailable" in detail["stop_reason"]
    events = (await client.get(f"/api/runs/{run_id}/events")).json()
    assert "REPOSITORY_READY" not in [e["type"] for e in events]


async def test_queue_claims_a_run_exactly_once(e2e) -> None:
    """Two workers racing for one job: exactly one wins."""
    from patchpilot.db.session import session_scope
    from patchpilot.worker.queue import claim_next_run

    client, settings = e2e
    await client.post(
        "/api/runs", json={"issue": "a" * 30, "fixture": "calculator_bug"}
    )

    async def claim(worker: str):
        async with session_scope(settings) as session:
            return await claim_next_run(session, worker)

    first, second = await asyncio.gather(claim("worker-1"), claim("worker-2"))
    claimed = [r for r in (first, second) if r is not None]
    assert len(claimed) == 1, "a run must be claimed by exactly one worker"


async def test_cancelling_a_queued_run(e2e) -> None:
    client, _ = e2e
    run_id = (
        await client.post(
            "/api/runs", json={"issue": "a" * 30, "fixture": "calculator_bug"}
        )
    ).json()["id"]

    assert (await client.post(f"/api/runs/{run_id}/cancel")).status_code == 202
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["cancel_requested"] is True
    # Still queued, so cancelled outright: no worker may pick it up and pay
    # for model calls before a heartbeat notices.
    assert detail["state"] == "CANCELLED"
    assert (await client.post(f"/api/runs/{run_id}/cancel")).status_code == 409


async def test_a_run_cancelled_after_its_claim_never_starts(e2e, monkeypatch) -> None:
    """The window between a worker's claim and the agent starting."""
    from patchpilot.db.models import Run
    from patchpilot.db.session import session_scope

    client, settings = e2e

    def must_not_run(messages, index):
        raise AssertionError("a cancelled run must not reach the model")

    install_provider(monkeypatch, must_not_run)
    run_id = (
        await client.post("/api/runs", json={"issue": "a" * 30, "fixture": "calculator_bug"})
    ).json()["id"]
    async with session_scope(settings) as session:
        run = await session.get(Run, run_id)
        run.state = "PREPARING"  # as claim_next_run leaves it
        run.cancel_requested = True

    await execute_run(run_id, settings)
    detail = (await client.get(f"/api/runs/{run_id}")).json()
    assert detail["state"] == "CANCELLED"
    assert detail["stop_reason"] == "cancelled before it started"


async def test_fixture_tasks_all_run_end_to_end(e2e, monkeypatch) -> None:
    """Definition of done item 12: the provided fixture tasks run successfully."""
    client, settings = e2e

    def recon(messages, index):
        if index == 0:
            return ScriptedTurn(tool_calls=[("list_files", {"path": ".", "depth": 2})])
        if index == 1:
            return ScriptedTurn(tool_calls=[("run_tests", {})])
        return ScriptedTurn(
            tool_calls=[("finish", {"resolved": False, "summary": "reconnaissance only"})]
        )

    install_provider(monkeypatch, recon)

    for fixture in ("calculator_bug", "auth_token_bug", "pagination_bug"):
        run_id = (
            await client.post(
                "/api/runs",
                json={"issue": f"the {fixture} issue needs investigating", "fixture": fixture},
            )
        ).json()["id"]
        await execute_run(run_id, settings)

        detail = (await client.get(f"/api/runs/{run_id}")).json()
        assert detail["state"] == "FAILED", f"{fixture}: {detail['stop_reason']}"

        tests = (await client.get(f"/api/runs/{run_id}/tests")).json()
        assert len(tests) == 1, fixture
        # Each fixture has a genuine bug, so its suite must be red at baseline.
        assert tests[0]["failed"] > 0, f"{fixture} should have failing tests"


async def test_benchmark_runs_are_not_claimable_by_the_generic_worker(e2e) -> None:
    """A benchmark task is executed inline by the benchmark runner.

    When the generic worker could also claim it, two processes ran the same
    task concurrently with different configuration — the worker used the
    default sandbox image instead of the per-instance one, and 15 simultaneous
    clones of the same repo corrupted each other
    ("fetch-pack: invalid index-pack output").
    """

    from patchpilot.db.models import BenchmarkRun, BenchmarkTask, Run
    from patchpilot.db.session import session_scope
    from patchpilot.domain.enums import RunState
    from patchpilot.ids import new_id, short_code
    from patchpilot.worker.queue import claim_next_run

    client, settings = e2e

    async with session_scope(settings) as session:
        benchmark = BenchmarkRun(id=new_id(), name="b", run_key="k", model="m")
        session.add(benchmark)
        await session.flush()
        task = BenchmarkTask(
            id=new_id(),
            benchmark_id=benchmark.id,
            instance_id="a__a-1",
            repo="o/r",
            base_commit="abc",
            problem_statement="p",
        )
        session.add(task)
        await session.flush()
        # A benchmark-owned run, left PENDING to model the worst case.
        session.add(
            Run(
                id=new_id(),
                code=short_code(),
                repository="o/r",
                issue="x" * 20,
                state=str(RunState.PENDING),
                benchmark_task_id=task.id,
            )
        )

    async with session_scope(settings) as session:
        assert await claim_next_run(session, "worker-1") is None, (
            "the generic worker must not claim a benchmark task's run"
        )

    # An ordinary run is still claimable.
    await client.post("/api/runs", json={"issue": "a" * 30, "fixture": "calculator_bug"})
    async with session_scope(settings) as session:
        claimed = await claim_next_run(session, "worker-1")
        assert claimed is not None
        assert claimed.benchmark_task_id is None
