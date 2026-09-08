# PatchPilot — Architecture

PatchPilot is an autonomous software-engineering agent. Given a repository and a
bug report, it explores the code, forms a hypothesis, edits files, runs tests,
reads the failures, and iterates until the tests pass or a budget is exhausted.
It then reports a unified diff, a structured trace, and metrics.

The same agent is driven by a SWE-bench Lite benchmark runner, so its
performance is measured by the official SWE-bench evaluator rather than by
hand-picked demos.

---

## 1. Layering

Each layer depends only on the layers below it. Nothing below the API layer
depends on FastAPI, and nothing below `db/` depends on the database.

```
  +--------------------------------------------------------------+
  |  frontend/            Next.js 15 - TypeScript - Tailwind      |
  +------------------------------+-------------------------------+
                                 | REST + SSE
  +------------------------------v-------------------------------+
  |  api/                 FastAPI routers, DTO schemas            |
  +--------------------------------------------------------------+
  |  worker/              DB-backed job queue, run executor       |
  +--------------------------------------------------------------+
  |  runs/  bench/        Services: run lifecycle, SWE-bench      |
  +--------------------------------------------------------------+
  |  agent/               Orchestrator - state machine - budgets  |
  |                       anti-loop - transcript compaction       |
  +---------------+----------------------+-----------------------+
  | providers/    | tools/               | events/               |
  | Anthropic     | Registry + policy    | Structured event bus  |
  | OpenAI        | native | MCP         | (persist + fan-out)   |
  | Scripted      |                      |                       |
  +---------------+----------------------+-----------------------+
  |  workspace/           Git clone/checkout, path jail, diff     |
  +--------------------------------------------------------------+
  |  sandbox/             Command execution: Local | Docker       |
  +--------------------------------------------------------------+
  |  db/                  SQLAlchemy 2 async - SQLite | Postgres  |
  +--------------------------------------------------------------+
```

## 2. The autonomous loop

```
                       issue + repo + base_commit
                                   |
                    +--------------v--------------+
                    |  prepare workspace (git)    |  PREPARING
                    +--------------+--------------+
                                   |
                    +--------------v--------------+
                    |  build system prompt +      |  ANALYZING
                    |  tool schemas (policy set)  |
                    +--------------+--------------+
                                   |
      #============================v============================#
      # while budgets remain:                                   #
      #                                                         #
      #   1. model call  -->  text summary + tool calls         #
      #   2. anti-loop check on (tool, canonical args)          #
      #   3. execute tools through the registry                 #
      #        native --> workspace / sandbox                   #
      #        mcp    --> MCP client --> MCP server             #
      #   4. append observations (truncated, capped)            #
      #   5. emit events; persist state transition              #
      #   6. if repetition/stall detected --> inject recovery   #
      #   7. if `finish` called and tests green --> stop        #
      #============================+============================#
                                   |
                    +--------------v--------------+
                    | final `git diff` = patch    |  RESOLVED / FAILED
                    | + trace + metrics           |  / TIMEOUT
                    +-----------------------------+
```

The loop never stops at "generate one patch". Every patch is followed by a test
run, every test run is followed by an observation, and every failed observation
feeds the next model call.

## 3. Agent state machine

`PENDING -> PREPARING -> ANALYZING -> {SEARCHING, READING, HYPOTHESIZING,
PATCHING, TESTING, REFLECTING}* -> {RESOLVED, FAILED, TIMEOUT, CANCELLED}`

The working state is derived from the tool the agent just chose (`search_code`
-> `SEARCHING`, `run_tests` -> `TESTING`, ...). Every transition is persisted as
a `STATE_CHANGED` event so the UI can replay a run exactly.

## 4. Tool system

One interface, two origins:

```
                Agent
                  |  (JSON schemas, names, results)
             ToolRegistry ----- ToolPolicy (allow / deny, default-deny)
             /          \
     NativeTool        McpTool --> MCP client --> MCP server(s)
        |
   Workspace + Sandbox
```

`Tool` declares: `name`, `description`, a Pydantic `args_model`, `timeout_s`,
and `async run(ctx, args) -> ToolResult`. The agent only ever sees the
interface, so an MCP-provided `read_file` and the native `read_file` are
indistinguishable to it — and both are subject to the same policy.

**Who decides what:** the MCP server decides which tools *exist*; PatchPilot's
policy decides which tools this agent is *allowed* to call; the model decides
which allowed tool to call *now*.

## 5. Execution isolation

`Sandbox` is a protocol: `exec(argv, cwd, timeout, env) -> ExecResult`.

* `DockerSandbox` — one container per run, `--network none`, `--memory`,
  `--cpus`, `--pids-limit`, no host mounts except the workspace, scrubbed
  environment. This is the correct posture for untrusted repository code.
* `LocalSandbox` — subprocess with a cwd jail, an environment allow-list,
  wall-clock timeouts, output byte caps, and process-tree kill. Used for
  fixtures and for development on machines without a Docker daemon.

The active sandbox is chosen by configuration; the agent code is identical.

## 6. Benchmark path

```
 SWE-bench Lite (HuggingFace)
        |  dataset.py — load instances, split, subset
        v
 bench/runner.py — per instance: workspace at base_commit -> agent run
        |           -> capture `git diff` -> predictions.jsonl
        v
 bench/evaluator.py — official harness:
        python -m swebench.harness.run_evaluation
          --dataset_name princeton-nlp/SWE-bench_Lite
          --predictions_path predictions.jsonl --run_id <id>
        v
 bench/report.py — parse logs/evaluation/<run_id>/**/report.json + results.json
        v
 persisted BenchmarkRun + BenchmarkTask rows -> dashboard
```

`agent_status` (the agent's own tests went green) and `benchmark_status` (the
official evaluator said RESOLVED) are stored in **separate columns** and
rendered separately. Only `benchmark_status` feeds the headline resolve rate.

## 7. Persistence & events

SQLAlchemy 2.0 async ORM. `sqlite+aiosqlite` for local dev,
`postgresql+asyncpg` for deployment — the only difference is `DATABASE_URL`.

Events are rows in an append-only `events` table with a monotonic per-run `seq`.
The SSE endpoint streams `seq > cursor`, so a reconnecting browser resumes
exactly where it left off, and a finished run replays identically from storage.
No Redis, no in-memory-only state.

## 8. Data model

```
Run --+--< Event        (seq, type, payload, iteration, ts)
      +--< ToolCall     (iteration, tool, args, ok, duration_ms, summary)
      +--< Hypothesis   (iteration, cause, evidence, files, fix, validation)
      +--< PatchAttempt (iteration, diff, files_changed, added, removed)
      +--< TestRun      (iteration, command, passed, failed, duration, output)

BenchmarkRun --< BenchmarkTask --(1:1)-- Run
```

## 9. Decisions

Recorded in `docs/decisions/`. Highlights:

| # | Decision |
|---|---|
| 0001 | Events persisted in SQL + SSE cursor streaming, instead of Redis pub/sub |
| 0002 | DB-backed job queue with atomic claim, instead of Celery/RQ |
| 0003 | `create_all` for dev; schema kept Alembic-ready for Postgres |
| 0004 | Docker sandbox for untrusted code, local sandbox for fixtures |
| 0005 | Official SWE-bench harness is authoritative; never re-implement grading |
