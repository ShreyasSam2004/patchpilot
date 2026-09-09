"""PatchPilot command line.

Everything the UI can do is available here, which keeps the CLI honest: it uses
the same services, the same agent, and the same evaluator.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .agent.orchestrator import AgentTask
from .agent.session import SessionConfig, run_agent_task
from .config import get_settings
from .db.session import dispose_db, init_db, session_scope
from .domain.enums import EventType, RunState
from .events.bus import Event, MemorySink
from .fixtures import get_fixture, list_fixtures
from .ids import new_id
from .logging_setup import configure_logging

app = typer.Typer(
    name="patchpilot",
    help="Autonomous software-engineering agent with SWE-bench Lite evaluation.",
    no_args_is_help=True,
    add_completion=False,
)
bench_app = typer.Typer(help="SWE-bench Lite benchmarking.", no_args_is_help=True)
tools_app = typer.Typer(help="Inspect the tool registry and MCP servers.", no_args_is_help=True)
app.add_typer(bench_app, name="bench")
app.add_typer(tools_app, name="tools")

console = Console()

STATE_STYLE = {
    RunState.RESOLVED: "bold green",
    RunState.FAILED: "bold red",
    RunState.TIMEOUT: "bold yellow",
    RunState.CANCELLED: "dim",
}


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    settings = get_settings()
    configure_logging("DEBUG" if verbose else "WARNING", settings.log_json)


@app.command()
def version() -> None:
    """Show the version."""
    console.print(f"PatchPilot {__version__}")


@app.command()
def doctor() -> None:
    """Check that everything a real run needs is actually available."""
    from .bench.dataset import dataset_available
    from .bench.evaluator import evaluator_preflight
    from .providers.factory import provider_status
    from .sandbox.factory import preflight as sandbox_preflight

    settings = get_settings()

    async def gather() -> dict[str, Any]:
        return {
            "provider": provider_status(settings),
            "sandbox": await sandbox_preflight(settings),
            "swebench": await evaluator_preflight(settings),
        }

    checks = asyncio.run(gather())
    dataset_ok, dataset_detail = dataset_available()

    table = Table(title="PatchPilot environment", show_lines=False)
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    def row(name: str, ok: bool, detail: str) -> None:
        # Details contain things like "pip install -e '.[swebench]'"; Rich would
        # parse the brackets as markup and silently delete them.
        table.add_row(name, "[green]OK[/]" if ok else "[red]MISSING[/]", escape(detail))

    provider = checks["provider"]
    row(
        f"Model ({provider['provider']})",
        bool(provider["ready"]),
        f"{provider['model']} — {provider['detail']}",
    )
    sandbox = checks["sandbox"]
    docker_usable = bool(sandbox["docker_available"]) and bool(
        sandbox.get("docker_image_present")
    )
    row(
        f"Sandbox ({settings.sandbox})",
        settings.sandbox != "docker" or docker_usable,
        f"docker daemon: {sandbox['docker_detail']}",
    )
    row(
        "Sandbox image",
        bool(sandbox.get("docker_image_present")),
        f"{sandbox.get('docker_image')} "
        + (
            "present"
            if sandbox.get("docker_image_present")
            else "- build it with: make docker-image"
        ),
    )
    swebench = checks["swebench"]
    row("SWE-bench harness", bool(swebench["harness_installed"]), "pip install -e '.[swebench]'")
    row("SWE-bench dataset", dataset_ok, dataset_detail)
    row(
        "Docker (for benchmarks)",
        bool(swebench["docker_available"]),
        swebench["docker_detail"],
    )
    free = swebench.get("free_disk_gb")
    row(
        "Disk space",
        free is None or free >= 50,
        f"{free} GB free (SWE-bench docs recommend ~120 GB)" if free else "unknown",
    )
    console.print(table)

    if swebench["problems"]:
        console.print(
            Panel(
                "\n".join(f"• {escape(p)}" for p in swebench["problems"]),
                title="Benchmarking is not fully available",
                border_style="yellow",
            )
        )


@app.command("fixtures")
def show_fixtures() -> None:
    """List the built-in fixture bug tasks."""
    table = Table(title="Fixture bug tasks")
    table.add_column("id")
    table.add_column("difficulty")
    table.add_column("title", overflow="fold")
    table.add_column("expected failures", justify="right")
    for fixture in list_fixtures():
        table.add_row(
            fixture.id,
            fixture.difficulty,
            fixture.title,
            str(len(fixture.expected_failing_tests)),
        )
    console.print(table)


@app.command()
def run(
    issue: str = typer.Option("", "--issue", "-i", help="Issue text, or @path/to/file."),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Local repository path."),
    url: str | None = typer.Option(None, "--url", "-u", help="Repository URL to clone."),
    fixture: str | None = typer.Option(None, "--fixture", "-f", help="Built-in fixture id."),
    base_commit: str | None = typer.Option(None, "--base-commit", "-b"),
    test_command: str | None = typer.Option(None, "--test-command", "-t"),
    model: str | None = typer.Option(None, "--model", "-m"),
    provider: str | None = typer.Option(None, "--provider", "-p"),
    sandbox: str | None = typer.Option(None, "--sandbox", "-s", help="local | docker"),
    max_iterations: int | None = typer.Option(None, "--max-iterations"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    show_patch: bool = typer.Option(True, "--patch/--no-patch"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Run the agent on a repository and print the result.

    Executes inline (no worker or database required), which makes this the
    fastest way to watch the loop work.
    """
    settings = get_settings()

    local_path: Path | None = None
    if fixture:
        task_fixture = get_fixture(fixture)
        local_path = task_fixture.repo_path
        issue = issue or task_fixture.issue
        test_command = test_command or task_fixture.test_command
        repository = task_fixture.repository
    elif repo:
        local_path = Path(repo).expanduser().resolve()
        repository = local_path.name
    elif url:
        repository = url.rstrip("/").removesuffix(".git").split("/")[-1]
    else:
        console.print("[red]Provide one of --fixture, --repo or --url.[/]")
        raise typer.Exit(2)

    if issue.startswith("@"):
        issue = Path(issue[1:]).read_text(encoding="utf-8")
    if not issue.strip():
        console.print("[red]An issue description is required (--issue).[/]")
        raise typer.Exit(2)

    task = AgentTask(
        run_id=new_id(),
        issue=issue,
        repository=repository,
        repo_url=url,
        local_path=local_path,
        base_commit=base_commit,
        test_command=test_command,
        budget_overrides={"max_iterations": max_iterations} if max_iterations else {},
    )
    config = SessionConfig(
        provider=provider, model=model, sandbox=sandbox, keep_workspace=keep_workspace
    )
    sink = MemorySink()

    console.print(
        Panel(
            f"[bold]{repository}[/]\n{issue.strip().splitlines()[0][:120]}",
            title=f"PatchPilot run {task.run_id[:8]}",
            border_style="blue",
        )
    )

    result = asyncio.run(
        run_agent_task(task, settings=settings, config=config, sinks=[sink])
    )

    if json_output:
        console.print_json(json.dumps(result.to_dict()))
        raise typer.Exit(0 if result.resolved else 1)

    console.print()
    _print_timeline(sink.events)
    console.print()
    _print_result(result, show_patch=show_patch)
    raise typer.Exit(0 if result.resolved else 1)


def _print_timeline(events: list[Event]) -> None:
    console.print("[bold]AGENT TIMELINE[/]")
    for event in events:
        payload = event.payload
        if event.type is EventType.REPOSITORY_READY:
            console.print(f"  [green]✓[/] repository prepared ({payload.get('sandbox')} sandbox)")
        elif event.type is EventType.ISSUE_ANALYZED:
            count = len(payload.get("tools_available", []))
            console.print(f"  [green]✓[/] issue analysed, {count} tools available")
        elif event.type is EventType.TOOL_RESULT:
            mark = "[green]✓[/]" if payload.get("ok") else "[red]✗[/]"
            summary = payload.get("summary", "")[:110]
            console.print(f"  {mark} [cyan]{payload.get('tool')}[/] {summary}")
        elif event.type is EventType.HYPOTHESIS_CREATED:
            console.print(f"  [magenta]●[/] hypothesis: {payload.get('cause', '')[:110]}")
        elif event.type is EventType.TEST_FINISHED:
            colour = "green" if payload.get("green") else "red"
            console.print(
                f"      [{colour}]{payload.get('passed', 0)} passed, "
                f"{payload.get('failed', 0)} failed[/]"
            )
        elif event.type is EventType.LOOP_DETECTED:
            console.print(f"  [yellow]⟳[/] loop detected: {payload.get('detail', '')}")
        elif event.type is EventType.BUDGET_EXCEEDED:
            console.print(f"  [yellow]⏱[/] budget exceeded: {payload.get('limit')}")


def _print_result(result, show_patch: bool) -> None:
    style = STATE_STYLE.get(result.state, "bold")
    console.print(f"[{style}]{result.state}[/] — {result.stop_reason}")

    metrics = result.metrics
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("iterations", str(metrics.iterations))
    table.add_row("tool calls", str(metrics.tool_calls))
    table.add_row("files inspected", str(metrics.files_inspected))
    table.add_row("files modified", str(metrics.files_modified))
    table.add_row("test runs", str(metrics.test_runs))
    table.add_row("hypotheses", str(metrics.hypotheses))
    table.add_row("runtime", f"{metrics.runtime_s:.1f}s")
    table.add_row("tokens", f"{metrics.input_tokens} in / {metrics.output_tokens} out")
    if metrics.cost_usd is not None:
        table.add_row("est. cost", f"${metrics.cost_usd:.4f}")
    console.print(table)

    last = result.last_test_run
    if last:
        colour = "green" if last.green else "red"
        console.print(
            f"\n[bold]FINAL TESTS[/] [{colour}]{last.passed} passed, "
            f"{last.failed} failed, {last.errors} errors[/]  ({last.command})"
        )

    if show_patch and result.final_patch.strip():
        console.print("\n[bold]PATCH[/]")
        console.print(Syntax(result.final_patch, "diff", theme="ansi_dark", line_numbers=False))
    elif show_patch:
        console.print("\n[dim]No patch produced.[/]")


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host"),
    port: int | None = typer.Option(None, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Start the API server (with an embedded worker by default)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "patchpilot.api.app:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command()
def worker() -> None:
    """Start a standalone background worker."""
    from .worker.__main__ import main as worker_main

    asyncio.run(worker_main())


@app.command("init-db")
def init_database() -> None:
    """Create database tables."""

    async def go() -> None:
        await init_db()
        await dispose_db()

    asyncio.run(go())
    console.print("[green]Database ready.[/]")


# ------------------------------------------------------------------- bench
@bench_app.command("list")
def bench_list(
    limit: int = typer.Option(20, "--limit", "-n"),
    offset: int = typer.Option(0, "--offset"),
) -> None:
    """List SWE-bench Lite instances."""
    from .bench.dataset import load_instances

    settings = get_settings()
    instances = load_instances(
        settings.swebench_dataset, settings.swebench_split, limit=limit, offset=offset
    )
    table = Table(title=f"{settings.swebench_dataset} [{offset}:{offset + limit}]")
    table.add_column("instance_id")
    table.add_column("repo")
    table.add_column("FAIL_TO_PASS", justify="right")
    for instance in instances:
        table.add_row(instance.instance_id, instance.repo, str(len(instance.fail_to_pass)))
    console.print(table)


@bench_app.command("run")
def bench_run(
    limit: int | None = typer.Option(None, "--limit", "-n", help="Number of instances."),
    offset: int = typer.Option(0, "--offset"),
    instance: list[str] = typer.Option([], "--instance", "-i", help="Explicit instance id."),
    name: str | None = typer.Option(None, "--name"),
    model: str | None = typer.Option(None, "--model", "-m"),
    provider: str | None = typer.Option(None, "--provider", "-p"),
    sandbox: str | None = typer.Option(None, "--sandbox", "-s"),
    max_iterations: int | None = typer.Option(None, "--max-iterations"),
    evaluate: bool = typer.Option(True, "--evaluate/--no-evaluate"),
) -> None:
    """Run a SWE-bench Lite batch end to end, then evaluate it officially."""
    from .bench.runner import create_benchmark, execute_benchmark

    settings = get_settings()
    if not limit and not instance:
        console.print("[red]Provide --limit or --instance.[/]")
        raise typer.Exit(2)

    async def go() -> str:
        await init_db(settings)
        async with session_scope(settings) as session:
            benchmark = await create_benchmark(
                session,
                name=name,
                limit=limit,
                offset=offset,
                instance_ids=list(instance) or None,
                provider=provider,
                model=model,
                sandbox=sandbox,
                max_iterations=max_iterations,
                evaluate=evaluate,
                settings=settings,
            )
            benchmark_id = benchmark.id
            console.print(
                f"[blue]Benchmark {benchmark_id[:8]}[/] — {benchmark.total_tasks} instances, "
                f"harness run_id={benchmark.run_key}"
            )
        await execute_benchmark(benchmark_id, settings)
        return benchmark_id

    benchmark_id = asyncio.run(go())
    asyncio.run(_show_benchmark(benchmark_id))


@bench_app.command("show")
def bench_show(benchmark_id: str) -> None:
    """Show a benchmark's results."""
    asyncio.run(_show_benchmark(benchmark_id))


@bench_app.command("evaluate")
def bench_evaluate(benchmark_id: str) -> None:
    """Re-run only the official evaluation for an existing batch."""
    from .bench.runner import reevaluate

    async def go() -> None:
        settings = get_settings()
        await init_db(settings)
        await reevaluate(benchmark_id, settings)
        await dispose_db()

    asyncio.run(go())
    asyncio.run(_show_benchmark(benchmark_id))


@bench_app.command("resume-bullet")
def bench_resume_bullet() -> None:
    """Print a resume bullet built from real, evaluator-confirmed results."""
    import sqlalchemy as sa

    from .db.models import BenchmarkRun

    async def go() -> None:
        settings = get_settings()
        await init_db(settings)
        async with session_scope(settings) as session:
            rows = (
                await session.execute(
                    sa.select(BenchmarkRun).where(BenchmarkRun.evaluated.is_(True))
                )
            ).scalars().all()
            graded = [b for b in rows if b.resolve_rate is not None]
            if not graded:
                console.print(
                    "[yellow]No officially-evaluated benchmark results exist yet.[/]\n"
                    "Run one first:  patchpilot bench run --limit 10"
                )
                return
            best = max(graded, key=lambda b: (b.resolve_rate or 0, b.total_tasks))
            attempted = best.resolved_tasks + best.unresolved_tasks
            console.print(
                Panel(
                    "Built PatchPilot, an autonomous software-engineering agent using LLM "
                    "tool-calling, MCP-compatible repository tools, iterative test-feedback "
                    "loops, and isolated code execution; resolved "
                    f"[bold]{best.resolve_rate}%[/] of {attempted} evaluated SWE-bench Lite "
                    f"tasks ({best.model}), graded by the official SWE-bench harness.",
                    title="Resume bullet (from real results)",
                    border_style="green",
                )
            )
        await dispose_db()

    asyncio.run(go())


async def _show_benchmark(benchmark_id: str) -> None:
    import sqlalchemy as sa

    from .db.models import BenchmarkRun, BenchmarkTask

    settings = get_settings()
    await init_db(settings)
    async with session_scope(settings) as session:
        benchmark = await session.get(BenchmarkRun, benchmark_id)
        if benchmark is None:
            console.print(f"[red]Benchmark {benchmark_id} not found.[/]")
            return
        tasks = (
            await session.execute(
                sa.select(BenchmarkTask)
                .where(BenchmarkTask.benchmark_id == benchmark_id)
                .order_by(BenchmarkTask.instance_id)
            )
        ).scalars().all()

        header = Table(show_header=False, box=None, padding=(0, 2))
        header.add_row("dataset", f"{benchmark.dataset} [{benchmark.split}]")
        header.add_row("model", f"{benchmark.provider} / {benchmark.model}")
        header.add_row("harness run_id", benchmark.run_key)
        header.add_row("status", benchmark.status)
        header.add_row("tasks attempted", str(benchmark.total_tasks))
        if benchmark.evaluated:
            header.add_row("resolved", f"[green]{benchmark.resolved_tasks}[/]")
            header.add_row("unresolved", str(benchmark.unresolved_tasks))
            header.add_row("[bold]resolve rate[/]", f"[bold green]{benchmark.resolve_rate}%[/]")
        else:
            header.add_row(
                "resolve rate",
                "[yellow]not evaluated — the official harness has not graded this batch[/]",
            )
            if benchmark.error:
                header.add_row("reason", benchmark.error)
        console.print(Panel(header, title=f"Benchmark {benchmark_id[:8]}", border_style="blue"))

        table = Table(title="Tasks")
        table.add_column("instance_id")
        table.add_column("agent")
        table.add_column("benchmark")
        table.add_column("patch", justify="right")
        for task in tasks:
            verdict = task.benchmark_status
            colour = {"RESOLVED": "green", "UNRESOLVED": "red", "ERROR": "yellow"}.get(
                verdict, "dim"
            )
            table.add_row(
                task.instance_id,
                task.agent_status,
                f"[{colour}]{verdict}[/]",
                f"{len(task.model_patch or '')}B",
            )
        console.print(table)
    await dispose_db()


# ------------------------------------------------------------------- tools
@tools_app.command("list")
def tools_list() -> None:
    """Show every tool the agent can see, and whether policy allows it."""
    from .agent.session import build_registry

    async def go() -> list[dict[str, Any]]:
        settings = get_settings()
        registry, sessions = await build_registry(settings)
        for session in sessions:
            await session.close()
        return registry.inventory()

    inventory = asyncio.run(go())
    table = Table(title="Tool registry")
    table.add_column("tool")
    table.add_column("origin")
    table.add_column("allowed")
    table.add_column("reason", overflow="fold")
    for entry in inventory:
        table.add_row(
            entry["name"],
            entry["origin"] + (f" ({entry['server']})" if entry["server"] else ""),
            "[green]yes[/]" if entry["allowed"] else "[red]no[/]",
            entry["reason"],
        )
    console.print(table)


@tools_app.command("mcp")
def tools_mcp() -> None:
    """Probe configured MCP servers and show the policy verdict per tool."""
    from .tools.mcp.loader import probe_servers

    results = asyncio.run(probe_servers(get_settings()))
    if not results:
        console.print(
            "[yellow]No MCP servers configured.[/] "
            f"Expected a config at {get_settings().mcp_config_path}"
        )
        return
    for server in results:
        console.print(
            Panel(
                f"transport={server['transport']} status={server['status']}\n"
                + "\n".join(
                    f"  {'[green]allow[/]' if t['allowed'] else '[red]deny [/]'} {t['name']}"
                    for t in server["tools"]
                )
                or "  (no tools)",
                title=f"MCP server: {server['name']}",
            )
        )


if __name__ == "__main__":
    app()
