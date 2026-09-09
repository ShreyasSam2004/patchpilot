# PatchPilot

**An autonomous software-engineering agent.** Give it a repository and a bug
report; it explores the code, forms a hypothesis, writes a patch, runs the test
suite in an isolated sandbox, reads the failures, and iterates until the tests
pass or a budget is exhausted.

Its performance is measured by the **official SWE-bench Lite evaluator**, not by
its own opinion of its work.

```
$ patchpilot run --fixture calculator_bug

✓ repository prepared        local sandbox
✓ search_code                'divide' — 4 matches in 2 files
✓ read_file                  src/calculator.py (36 lines)
● hypothesis                 divide() performs a / b with no zero guard
✓ edit_file                  src/calculator.py
✓ run_tests                  pytest tests/ -q
    7 passed, 3 failed
● analysing failures…        REGRESSION
✓ edit_file                  src/calculator.py
✓ run_tests                  pytest tests/ -q
    10 passed, 0 failed
✓ RESOLVED — 6 iterations, 1 patch, 2 test runs
```

---

## Contents

- [What it is](#what-it-is)
- [Why it exists](#why-it-exists)
- [Architecture](#architecture)
- [The agent loop](#the-agent-loop)
- [Tools and MCP](#tools-and-mcp)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running locally](#running-locally)
- [Running the fixture tasks](#running-the-fixture-tasks)
- [Running SWE-bench Lite](#running-swe-bench-lite)
- [Why SWE-bench?](#why-swe-bench)
- [Evaluation methodology](#evaluation-methodology)
- [Results](#results)
- [Screenshots](#screenshots)
- [Testing](#testing)
- [Security considerations](#security-considerations)
- [Limitations](#limitations)
- [Future improvements](#future-improvements)

---

## What it is

PatchPilot takes three inputs — a repository, an issue, and optionally a base
commit — and produces a validated unified diff plus a complete, replayable
record of how it got there.

It is **not** a chat interface. The engineering problem it solves is the
*autonomous loop*: tool selection, state management, test feedback, patch
validation, repetition recovery, budget enforcement, observability, and
reproducible benchmark evaluation.

| | |
|---|---|
| **Backend** | Python 3.11+ · FastAPI · SQLAlchemy 2 (async) · SQLite/PostgreSQL |
| **Frontend** | Next.js 15 · TypeScript · Tailwind CSS 4 |
| **Models** | Anthropic · OpenAI (pluggable `ModelProvider`) |
| **Tools** | 14 native tools + any MCP server, behind one permission policy |
| **Isolation** | Docker container per run, or a hardened local subprocess sandbox |
| **Benchmark** | SWE-bench Lite via the official `swebench` harness |

## Why it exists

Most "AI fixes your bug" demos generate a patch and stop. That is the easy 20%.
The hard part is everything after the first patch fails:

- **Knowing that it failed.** Exit codes are not enough — a collection error and
  two hundred assertion failures both exit non-zero. PatchPilot parses the test
  runner's own summary in every mode it emits (including `pytest -q`, which
  prints no banner).
- **Reacting usefully.** The agent must classify the failure — original bug,
  regression, wrong assumption, unrelated, environment — and act on the
  classification instead of guessing at a new patch.
- **Not looping.** The characteristic failure of an autonomous coding agent is a
  confident loop: re-reading the same file, re-running the same failing test
  without changing anything, re-applying a patch it already tried. PatchPilot
  detects all three and injects a re-plan directive.
- **Not lying.** `finish(resolved=true)` is refused unless a test run actually
  went green *and* the working tree actually contains a diff.
- **Being measurable.** A demo proves nothing. A benchmark score from an
  evaluator you did not write proves something.

## Architecture

Each layer depends only on the layers below it. Nothing below the API layer
imports FastAPI; nothing below `db/` knows there is a database.

```
  ┌──────────────────────────────────────────────────────────────┐
  │  frontend/            Next.js · TypeScript · Tailwind        │
  └──────────────────────────────┬───────────────────────────────┘
                                 │  REST + Server-Sent Events
  ┌──────────────────────────────▼───────────────────────────────┐
  │  api/                 FastAPI routers · DTO schemas          │
  ├──────────────────────────────────────────────────────────────┤
  │  worker/              DB-backed job queue · run executor     │
  ├──────────────────────────────────────────────────────────────┤
  │  runs/  bench/        Run lifecycle · SWE-bench batches      │
  ├──────────────────────────────────────────────────────────────┤
  │  agent/               Orchestrator · state machine · budgets │
  │                       anti-loop · transcript compaction      │
  ├───────────────┬──────────────────────┬───────────────────────┤
  │ providers/    │ tools/               │ events/               │
  │ Anthropic     │ Registry + policy    │ Append-only event log │
  │ OpenAI        │ native │ MCP         │ (persist + fan-out)   │
  ├───────────────┴──────────────────────┴───────────────────────┤
  │  workspace/           Git clone/checkout · path jail · diff  │
  ├──────────────────────────────────────────────────────────────┤
  │  sandbox/             Command execution: Docker │ Local      │
  ├──────────────────────────────────────────────────────────────┤
  │  db/                  SQLAlchemy 2 async · SQLite│PostgreSQL │
  └──────────────────────────────────────────────────────────────┘
```

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## The agent loop

```
                       issue + repository + base_commit
                                    │
                     ┌──────────────▼──────────────┐
                     │  prepare workspace (git)    │   PREPARING
                     └──────────────┬──────────────┘
                                    │
                     ┌──────────────▼──────────────┐
                     │  system prompt + tool set   │   ANALYZING
                     │  (policy-filtered schemas)  │
                     └──────────────┬──────────────┘
                                    │
   ╔════════════════════════════════▼════════════════════════════════╗
   ║  while budget remains:                                          ║
   ║                                                                 ║
   ║   1. model call ─────► short status + tool calls                ║
   ║   2. anti-loop check on (tool, canonical args)                  ║
   ║   3. execute tools through the registry                         ║
   ║          native ──► workspace / sandbox                         ║
   ║          mcp    ──► MCP client ──► MCP server                   ║
   ║   4. append observations (capped, older ones compacted)         ║
   ║   5. emit events; persist the state transition                  ║
   ║   6. repetition detected? ──► inject a re-plan directive        ║
   ║   7. finish() called and tests green? ──► stop                  ║
   ╚════════════════════════════════┬════════════════════════════════╝
                                    │
                     ┌──────────────▼──────────────┐
                     │  git diff = final patch     │   RESOLVED / FAILED
                     │  + trace + metrics          │   / TIMEOUT
                     └─────────────────────────────┘
```

**States** — `PENDING → PREPARING → ANALYZING → {SEARCHING, READING,
HYPOTHESIZING, PATCHING, TESTING, REFLECTING}* → {RESOLVED, FAILED, TIMEOUT,
CANCELLED}`. Every transition is persisted as an event, so the UI renders a live
run and a finished run through exactly the same code path.

**Budgets** — `MAX_ITERATIONS`, `MAX_TOOL_CALLS`, `MAX_RUNTIME`,
`MAX_TEST_RUNTIME`, `MAX_MODEL_COST`. The agent is told how much budget remains
so it can pace itself, and is warned before it is cut off.

**Recovery** — when the detector fires, the agent receives a factual digest of
what the run has established (files inspected, queries already tried, current
hypothesis, last test result) and is asked which of its assumptions is most
likely wrong before it may act again.

Full detail: [`docs/AGENT_LOOP.md`](docs/AGENT_LOOP.md).

## Tools and MCP

One interface, two origins. The agent cannot tell them apart, and does not
need to:

```
                     Agent
                       │   (names, JSON schemas, results)
                  ToolRegistry ──── ToolPolicy  (allow / deny, default-deny)
                  ╱          ╲
          NativeTool        McpTool ──► MCP client ──► MCP server(s)
             │
      Workspace + Sandbox
```

**The permission model is the point:**

| Who | Decides |
|---|---|
| The **MCP server** | what tools *exist* |
| **PatchPilot** | what tools this agent is *allowed to call* |
| The **model** | which allowed tool to call *now* |

Discovery never implies permission. Anything not on the allow-list is refused,
and an MCP tool that collides with a native name is re-registered as
`mcp__<server>__<tool>` so it can never shadow a trusted local implementation.

Native tools: `list_files` `read_file` `write_file` `search_code` `find_symbol`
`apply_patch` `edit_file` `git_diff` `git_status` `git_log` `run_tests`
`run_command` `record_hypothesis` `finish`.

Denied by default: `git_push` `git_commit` `delete_repository` `host_shell`
`credential_access` `network_fetch`.

Full detail: [`docs/MCP.md`](docs/MCP.md).

## Installation

**Requirements:** Python 3.11+, Node 18+, Git. Docker is required for SWE-bench
evaluation and recommended for running untrusted repositories.

```bash
git clone <this-repo> patchpilot && cd patchpilot

# Backend
cd backend
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev,anthropic]"

# Frontend
cd ../frontend && npm install
```

Optional extras: `.[openai]`, `.[mcp]`, `.[postgres]`, `.[swebench]`.

## Configuration

Everything comes from the environment. Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

```bash
PATCHPILOT_PROVIDER=anthropic
PATCHPILOT_MODEL=claude-sonnet-5
PATCHPILOT_ANTHROPIC_API_KEY=sk-ant-...   # or ANTHROPIC_API_KEY

PATCHPILOT_SANDBOX=local                  # local | docker
PATCHPILOT_MAX_ITERATIONS=30
PATCHPILOT_MAX_COST_USD=5.0

# Swap this one line to move to PostgreSQL:
# PATCHPILOT_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/patchpilot
```

Check that everything a real run needs is actually present:

```bash
patchpilot doctor
```

```
┌─────────────────────────┬─────────┬──────────────────────────────────┐
│ Model (anthropic)       │ OK      │ claude-sonnet-5 — API key present│
│ Sandbox (local)         │ OK      │ process isolation                │
│ SWE-bench harness       │ OK      │ swebench 5.0.2                   │
│ SWE-bench dataset       │ OK      │ datasets package available       │
│ Docker (for benchmarks) │ MISSING │ daemon unreachable               │
│ Disk space              │ MISSING │ 36 GB free (~120 GB recommended) │
└─────────────────────────┴─────────┴──────────────────────────────────┘
```

`doctor` tells you the truth about the machine you are on. Nothing is faked to
make the dashboard look complete.

## Running locally

```bash
# Terminal 1 — API + embedded worker
cd backend && patchpilot serve

# Terminal 2 — UI
cd frontend && npm run dev
```

Open <http://localhost:3000>.

For production, run the worker as its own process and turn the embedded one off:

```bash
PATCHPILOT_WORKER_ENABLED=false patchpilot serve
python -m patchpilot.worker
```

### Pages

| Route | Purpose |
|---|---|
| `/` | Product overview, environment warnings, best evaluated result |
| `/runs` | Run history |
| `/runs/new` | Start a run: fixture, repository URL, or local path |
| `/runs/[id]` | **Live execution workspace** — timeline, files, patch, tests, trace, metrics |
| `/benchmarks` | Benchmark history and evaluation preflight |
| `/benchmarks/[id]` | Benchmark dashboard with per-task drill-down |
| `/settings` | Effective configuration, tool registry, permission policy, MCP servers |

## Running the fixture tasks

Three tiny repositories with deliberately introduced bugs. They exercise the
entire loop in seconds, for a fraction of a cent — run these before spending
money on SWE-bench.

```bash
patchpilot fixtures                 # list them
patchpilot run --fixture calculator_bug
patchpilot run --fixture auth_token_bug
patchpilot run --fixture pagination_bug
```

| Fixture | Bug | Why it is interesting |
|---|---|---|
| `calculator_bug` | `divide()` raises on a zero denominator | Single guard clause; the docstring and return type already state the contract |
| `auth_token_bug` | Expired refresh token returns 500 instead of 401 | Two files; `ExpiredTokenError` is a *sibling* of `InvalidTokenError`, so the narrow `except` misses it. Fixing one endpoint and not the other leaves a test red — forcing a second iteration |
| `pagination_bug` | Final partial page is unreachable | Off-by-one from integer division. The naive `+ 1` fix breaks the empty-list case that currently passes — forcing the agent to react to a regression it caused |

Each fixture declares its expected failing tests in `task.json`, so a fixture
run is graded objectively rather than by eyeballing the diff.

## Running SWE-bench Lite

```bash
pip install -e ".[swebench]"

patchpilot bench list --limit 10          # browse the dataset
patchpilot bench run --limit 10           # generate patches, then evaluate
patchpilot bench show <benchmark-id>      # results
patchpilot bench evaluate <benchmark-id>  # re-grade an existing batch
patchpilot bench resume-bullet            # resume line from real numbers
```

Or from the UI: **Benchmarks → New benchmark run**.

The runner:

1. loads instances from `princeton-nlp/SWE-bench_Lite` (HuggingFace),
2. prepares each repository at its `base_commit`,
3. hands the agent **only** the problem statement and the repository,
4. captures `git diff` as `model_patch`,
5. writes `predictions.jsonl` in the harness's exact schema,
6. shells out to the official evaluator,
7. parses the harness's own report files,
8. persists per-task verdicts and batch aggregates.

## Why SWE-bench?

Because a hand-picked demo proves nothing about a system like this.

An agent that looks impressive on three curated bugs can fail completely on real
ones. SWE-bench Lite is 300 real issues from real Python projects (Django,
sympy, scikit-learn, astropy, …), each with a known-good patch and a test suite
that decides the outcome. The grading criterion is not "does the diff look
sensible" — it is "do the `FAIL_TO_PASS` tests now pass, and do the
`PASS_TO_PASS` tests still pass".

Crucially, **PatchPilot does not grade itself.** Re-implementing the grading
logic would produce a number that looks like a SWE-bench score but is not one.
The `swebench` harness is invoked as a subprocess and its own report files are
parsed. If the harness cannot run — no Docker, no disk — the batch is recorded
as `NOT EVALUATED` and the UI says so, rather than inventing a figure.

## Evaluation methodology

**Two verdicts, kept apart.** They are stored in separate columns and rendered
separately, because conflating them is how a project ends up quietly reporting
its own opinion as a benchmark result:

| | Meaning | Feeds the resolve rate? |
|---|---|---|
| `agent_status` | The agent's own tests went green | **No** |
| `benchmark_status` | The official evaluator said RESOLVED | **Yes** |

```
resolve_rate = resolved_tasks / (resolved_tasks + unresolved_tasks) × 100
```

`resolve_rate` is `None` — not `0.0` — until the harness has actually graded the
batch. An ungraded batch and a batch that scored zero are very different claims,
and the UI never conflates them.

**No answer-key leakage.** The agent receives the problem statement and the
repository at `base_commit`. It never receives `FAIL_TO_PASS`, `PASS_TO_PASS`, or
the gold patch. This is enforced by a test
(`test_the_agent_is_never_given_the_answer_key`) that greps everything reaching
the prompt for those values.

**Reproducibility.** Each batch persists its dataset, split, harness `run_id`,
provider, model, budgets, and instance selection (explicit ids, or a
deterministic `sorted[offset:offset+limit]` slice). Re-running from the stored
configuration selects exactly the same instances.

**Verified against the installed harness.** Report parsing is pinned to what
`swebench` 5.0.2 actually writes, confirmed by reading
`swebench.harness.reporting.make_run_report` and
`swebench.harness.grading.get_eval_report`:

- per-instance: `logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json`
- batch summary: `<model>.<run_id>.json`
- schema: `patch_is_None`, `patch_exists`, `patch_successfully_applied`,
  `resolved`, `infra_failure`

The parser also accepts the older `logs/evaluation/` layout, so a harness
upgrade or downgrade does not silently produce "no results". See
[`docs/SWEBENCH.md`](docs/SWEBENCH.md).

## Results

<!-- RESULTS -->

**No SWE-bench Lite batch has been evaluated in this repository yet.**

This section is intentionally empty. It will be filled in only from a real
`bench run` whose patches were graded by the official harness — the numbers come
from `patchpilot bench resume-bullet`, which reads the persisted
evaluator verdicts and refuses to print anything if none exist.

To produce them you need an API key, a running Docker daemon, and disk space:

```bash
patchpilot doctor                # confirm all three
patchpilot bench run --limit 10
patchpilot bench resume-bullet
```

<!-- /RESULTS -->

## Screenshots

Captured from the running application against a live backend and real agent
runs (`node frontend/scripts/screenshots.mjs`).

### The run workspace

![Run workspace](docs/screenshots/03-run-workspace.png)

Live timeline on the right, tabbed detail on the left. The timeline renders
only structured actions — tool calls, their outcomes, recorded hypotheses, test
results — with the hypothesis expanded inline. Model chain-of-thought is never
stored or shown.

### Reacting to a failed patch

![Test runs](docs/screenshots/06-run-tests.png)

Test run #1 is red (9 passed, 1 failed) because the first guard was too narrow.
The agent read the failure, corrected the fix, and run #2 is green. This is the
loop the project exists to build.

### The patch

![Patch](docs/screenshots/05-run-patch.png)

Server-parsed unified diff with old/new gutters, rendered or raw.

### Honest empty states

![Benchmarks](docs/screenshots/08-benchmarks.png)

The benchmark dashboard on a machine without Docker: it says exactly what is
missing and refuses to show a resolve rate rather than defaulting to 0%.

| | |
|---|---|
| [Landing](docs/screenshots/01-landing.png) | [New run](docs/screenshots/02-new-run.png) |
| [Agent trace](docs/screenshots/04-run-trace.png) | [Run history](docs/screenshots/07-runs-list.png) |
| [Settings](docs/screenshots/09-settings.png) | |

## Testing

```bash
cd backend
pytest                       # 213 hermetic tests
pytest -m live               # 6 more that hit the network (opt-in)
pytest -m "not integration"  # fast unit tests only
ruff check . && mypy patchpilot

cd ../frontend
npm run typecheck && npm run build
```

The suite runs against a **scripted model double**, so the entire loop —
exploration, hypothesis, patch, test, observe, iterate, recover, finish — is
exercised deterministically and for free. Everything else is real: real git
workspaces, real subprocesses, real pytest runs, a real database, a real
FastAPI app, the real worker.

| Area | What is covered |
|---|---|
| Sandbox | timeouts, process-tree kill, output caps, env scrubbing, command guard, path jail |
| Workspace | cross-platform path escapes, diff capture, patch application, reset |
| Tools | schemas, validation, policy, registry collisions, every tool's behaviour |
| Test parsing | pytest (default *and* `-q`), unittest, jest, timeouts, collection errors |
| Agent loop | multi-iteration fix, regression recovery, budgets, anti-loop, honest failure |
| Benchmark | harness report parsing (both layouts), prediction schema, no answer-key leakage |
| API | 28 endpoints, event ordering, resume cursors, secret redaction |
| End-to-end | HTTP → queue → worker → agent → events → API, and queue claim races |
| Live (opt-in) | The real SWE-bench Lite dataset, deterministic instance selection, and a real `git clone` + checkout at an exact commit |

## Security considerations

Repository code is treated as untrusted, because it is.

**Execution**
- Commands run as an argv vector — **never** through a shell. Pipes,
  redirection, `&&` and `$()` in arguments are refused outright.
- A command guard denies `sudo`, `ssh`, `docker`, `curl`/`wget`, credential
  paths (`.ssh`, `.aws`, `.env`, `.git-credentials`), recursive workspace
  deletes, and mutating git subcommands (`push`, `remote`, `config`, `clone`).
- Wall-clock timeouts with a **process-tree** kill — a plain `kill()` leaves
  pytest's children running.
- Output is read with a hard byte cap, so a runaway test cannot exhaust memory.

**Isolation**
- `DockerSandbox`: one container per run, `--network none`, `--memory`,
  `--cpus`, `--pids-limit`, `--cap-drop ALL`, `--security-opt
  no-new-privileges`, `--tmpfs /tmp:noexec,nosuid`, no host mounts except the
  workspace.
- `LocalSandbox`: working-directory jail, environment allow-list, timeouts,
  output caps. It provides **no kernel-level isolation** and says so — in
  `sandbox.info()`, in `/api/status`, on the landing page, and in Settings.

**Filesystem**
- Every model-supplied path goes through one jail that resolves symlinks and
  refuses anything outside the workspace. Paths are treated as rooted under
  *both* POSIX and Windows conventions, so `/etc/passwd` behaves identically on
  every platform.

**Secrets**
- Child processes get an environment rebuilt from an allow-list; anything whose
  name contains `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`… is
  dropped, as defence in depth.
- `/api/config` reduces every credential to `"set"` or `null`. A test asserts a
  planted key never appears in the response.
- MCP server environment **keys** are shown in the UI; values never are.

**Agent**
- Default-deny tool policy; `git push`/`commit` are absent from the registry
  *and* denied by policy.
- MCP tool descriptions are treated as untrusted text and truncated.
- No model chain-of-thought is ever stored — only structured actions,
  observations, explicitly recorded hypotheses, and the agent's short visible
  status line.

See [`docs/SECURITY.md`](docs/SECURITY.md).

## Limitations

- **A real run needs an API key.** Without one, the loop is provable only
  through the scripted test double. The UI labels any such run and refuses to
  present it as a real result.
- **SWE-bench evaluation needs Docker and ~120 GB of disk.** The official
  harness builds a container per instance. Without them, PatchPilot still
  generates patches but records the batch as `NOT EVALUATED`.
- **The local sandbox is not a security boundary.** Use Docker for repositories
  you do not trust.
- **Python-first.** The tools work on any language, but symbol search,
  test-output parsing and the default test command are tuned for Python (with
  best-effort jest/unittest support), matching SWE-bench Lite.
- **No repository-wide semantic index.** Localisation is lexical
  (`search_code` / `find_symbol`), which is fast and dependency-free but weaker
  than an LSP or embedding index on very large codebases.
- **Single-attempt patches.** There is no best-of-N sampling or patch ranking,
  both of which materially raise published SWE-bench scores.
- **`create_all` schema management.** Fine for this scope; Alembic is the
  documented next step (ADR-0003).

## Future improvements

- Best-of-N patch generation with test-based ranking.
- An LSP-backed `find_references` / `goto_definition` tool pair for stronger
  localisation.
- Reproduction-first workflow: write a failing test from the issue *before*
  patching, so the fix is validated against a test the agent derived.
- Streaming model responses so the timeline updates mid-turn.
- Parallel benchmark task execution (the harness already parallelises grading).
- Alembic migrations and a Postgres deployment target.
- HTTP/SSE MCP transports in addition to stdio.

---

## Project layout

```
backend/
  patchpilot/
    agent/        orchestrator · state · prompts · budgets · anti-loop · transcript
    api/          FastAPI app, routers, DTO schemas
    bench/        SWE-bench dataset · batch runner · official evaluator adapter
    db/           SQLAlchemy models and async session management
    events/       structured event bus and database recorder
    providers/    ModelProvider protocol · Anthropic · OpenAI · scripted double
    runs/         run lifecycle service
    sandbox/      Sandbox protocol · local · docker · command guard
    tools/        Tool interface · registry · policy · native/ · mcp/
    worker/       DB-backed queue and job execution
    workspace/    git workspace with a path jail
  fixtures/       three bug fixtures with task manifests
  tests/          182 tests
frontend/
  src/app/        routes: / /runs /runs/new /runs/[id] /benchmarks /benchmarks/[id] /settings
  src/components/ ui primitives, timeline, run panels
  src/lib/        typed API client, wire types, formatters
docs/             architecture · agent loop · MCP · SWE-bench · security · ADRs
```

## License

MIT
