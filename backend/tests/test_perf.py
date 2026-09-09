"""Guards on the things that made the UI feel slow.

Both regressions here are the same shape: an expensive operation performed to
answer a cheap question, on a path every page load hits.
"""

from __future__ import annotations

import subprocess
import sys

from patchpilot.bench.dataset import dataset_available


def test_dataset_check_does_not_import_datasets() -> None:
    """`import datasets` costs ~4s (pyarrow, pandas) to answer "is it there?".

    /api/benchmarks/preflight called it on every page load, which is why the
    benchmark page took 10 seconds to appear.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys;"
            "from patchpilot.bench.dataset import dataset_available;"
            "ok, _ = dataset_available();"
            "print(ok, 'datasets' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    installed, imported = probe.stdout.split()
    assert imported == "False", "dataset_available() must not import the package"
    assert installed in {"True", "False"}


def test_dataset_check_is_fast() -> None:
    import time

    started = time.perf_counter()
    for _ in range(20):
        dataset_available()
    assert time.perf_counter() - started < 1.0


def test_mcp_check_does_not_import_mcp() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys;"
            "from patchpilot.config import get_settings;"
            "from patchpilot.tools.mcp.loader import mcp_status;"
            "asyncio.run(mcp_status(get_settings()));"
            "print('mcp' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False"


async def test_docker_probes_are_cached() -> None:
    """`docker info` costs ~2s and was run several times per page load."""
    import patchpilot.sandbox.docker as docker_module

    calls: list[list[str]] = []
    original = docker_module._run_host

    async def counting(argv, timeout=60):
        calls.append(argv)
        return await original(argv, timeout)

    docker_module.invalidate_probe_cache()
    docker_module._run_host = counting  # type: ignore[assignment]
    try:
        for _ in range(5):
            await docker_module.docker_available()
            await docker_module.image_present("patchpilot/runner:latest")
        # One `docker info` and one `docker image inspect`, not five of each.
        assert len(calls) <= 2, [c[:3] for c in calls]
    finally:
        docker_module._run_host = original  # type: ignore[assignment]
        docker_module.invalidate_probe_cache()


async def test_invalidating_the_cache_forces_a_fresh_probe() -> None:
    """A pull or a build changes image presence; the cache must not hide it."""
    import patchpilot.sandbox.docker as docker_module

    calls: list[list[str]] = []
    original = docker_module._run_host

    async def counting(argv, timeout=60):
        calls.append(argv)
        return await original(argv, timeout)

    docker_module.invalidate_probe_cache()
    docker_module._run_host = counting  # type: ignore[assignment]
    try:
        await docker_module.image_present("patchpilot/runner:latest")
        docker_module.invalidate_probe_cache()
        await docker_module.image_present("patchpilot/runner:latest")
        assert len(calls) == 2
    finally:
        docker_module._run_host = original  # type: ignore[assignment]
        docker_module.invalidate_probe_cache()
