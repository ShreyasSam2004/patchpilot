"""Live SWE-bench path tests.

These hit the network: the real HuggingFace dataset and a real `git clone`.
They verify everything in the benchmark pipeline that does *not* require a
model API key or a Docker daemon — dataset loading, instance selection,
benchmark/task row creation, and repository preparation at a base commit.

They skip (rather than fail) when the network or an optional dependency is
unavailable, so the default suite stays hermetic.

    pytest -m live
"""

from __future__ import annotations

import pytest

from patchpilot.bench.dataset import dataset_available, load_instances
from patchpilot.bench.runner import build_agent_task, create_benchmark
from patchpilot.db.models import BenchmarkTask
from patchpilot.db.session import dispose_db, init_db, session_scope
from patchpilot.domain.enums import BenchmarkRunStatus, BenchmarkStatus
from patchpilot.sandbox.base import SandboxLimits
from patchpilot.sandbox.local import LocalSandbox
from patchpilot.workspace.workspace import prepare_workspace

pytestmark = [pytest.mark.live, pytest.mark.integration]

DATASET = "princeton-nlp/SWE-bench_Lite"


@pytest.fixture(scope="module")
def instances():
    available, detail = dataset_available()
    if not available:
        pytest.skip(detail)
    try:
        return load_instances(DATASET, "test", limit=5)
    except Exception as exc:
        pytest.skip(f"could not load {DATASET}: {exc}")


def test_the_real_dataset_loads(instances) -> None:
    assert len(instances) == 5
    for instance in instances:
        assert instance.instance_id
        assert "/" in instance.repo
        assert len(instance.base_commit) == 40
        assert instance.problem_statement.strip()
        assert instance.fail_to_pass, "every Lite instance has FAIL_TO_PASS tests"


def test_selection_is_deterministic() -> None:
    """A stored (offset, limit) must select the same instances every time."""
    first = load_instances(DATASET, "test", limit=5)
    second = load_instances(DATASET, "test", limit=5)
    assert [i.instance_id for i in first] == [i.instance_id for i in second]

    window = load_instances(DATASET, "test", limit=2, offset=3)
    assert [i.instance_id for i in window] == [i.instance_id for i in first[3:5]]


def test_explicit_instance_ids_are_honoured(instances) -> None:
    wanted = [instances[2].instance_id, instances[0].instance_id]
    selected = load_instances(DATASET, "test", instance_ids=wanted)
    assert [i.instance_id for i in selected] == wanted, "order is preserved"


def test_a_real_instance_produces_a_clean_agent_task(instances) -> None:
    instance = instances[0]
    task = build_agent_task(instance, "run-live-1")

    assert task.repo_url == f"https://github.com/{instance.repo}.git"
    assert task.base_commit == instance.base_commit
    assert task.issue == instance.problem_statement
    assert task.test_command

    # The answer key must not be anywhere in what the agent receives.
    reachable = " ".join(
        str(v) for v in (task.issue, task.hints, task.extra_guidance) if v
    )
    for node_id in instance.fail_to_pass + instance.pass_to_pass:
        assert node_id not in reachable
    if instance.patch:
        assert instance.patch not in reachable


async def test_creating_a_benchmark_from_the_real_dataset(settings, tmp_path) -> None:
    settings.database_url = f"sqlite+aiosqlite:///{(tmp_path / 'live.db').as_posix()}"
    settings.swebench_dataset = DATASET
    await dispose_db()
    await init_db(settings)
    try:
        async with session_scope(settings) as session:
            benchmark = await create_benchmark(
                session,
                name="live smoke",
                limit=3,
                evaluate=False,
                settings=settings,
            )
            benchmark_id = benchmark.id

            assert benchmark.total_tasks == 3
            assert benchmark.status == str(BenchmarkRunStatus.PENDING)
            assert benchmark.run_key.startswith("patchpilot-")
            assert benchmark.evaluated is False
            assert benchmark.resolve_rate is None, "nothing graded yet"
            assert benchmark.config["reproduce"]["dataset"] == DATASET
            assert benchmark.config["reproduce"]["selection"] == "sorted[0:3]"

        async with session_scope(settings) as session:
            import sqlalchemy as sa

            tasks = (
                await session.execute(
                    sa.select(BenchmarkTask).where(
                        BenchmarkTask.benchmark_id == benchmark_id
                    )
                )
            ).scalars().all()
            assert len(tasks) == 3
            for task in tasks:
                assert task.benchmark_status == str(BenchmarkStatus.PENDING)
                assert task.problem_statement.strip()
                assert len(task.base_commit) == 40
    finally:
        await dispose_db()


async def test_repository_preparation_checks_out_a_base_commit(tmp_path) -> None:
    """Clone and check out a real repository at an exact commit.

    Uses a small, stable public repository rather than a SWE-bench one, so the
    test verifies the mechanism without downloading hundreds of megabytes.
    """
    root = tmp_path / "clone"
    sandbox = LocalSandbox(root, SandboxLimits(timeout_s=300, max_output_bytes=200_000))

    probe = await sandbox.exec(["git", "ls-remote", "https://github.com/pallets/click.git", "HEAD"])
    if not probe.ok:
        pytest.skip(f"no network access to github: {probe.stderr[:120]}")

    head = probe.stdout.split()[0]
    workspace = await prepare_workspace(
        root,
        sandbox,
        repo_url="https://github.com/pallets/click.git",
        base_commit=head,
    )

    assert await workspace.head_commit() == head
    info = await workspace.info()
    assert info.dirty is False, "a freshly prepared workspace must be clean"
    assert (await workspace.diff()).strip() == "", "and must produce an empty diff"

    # The path jail still applies to a cloned repository.
    from patchpilot.errors import PathEscapeError

    with pytest.raises(PathEscapeError):
        workspace.resolve("../../etc/passwd")
