# SWE-bench Lite integration

## The rule

**PatchPilot does not grade its own patches.**

Grading a SWE-bench instance means: check out the repository at `base_commit`,
apply the model patch, apply the *test* patch, run `FAIL_TO_PASS` and
`PASS_TO_PASS` in the instance's own container, and decide. Re-implementing
that would produce a number that looks like a SWE-bench score but is not one.

So the official `swebench` harness is invoked as a subprocess and **its own
report files are parsed**. If the harness cannot run, the batch is recorded as
`NOT EVALUATED` and no resolve rate is displayed.

## Pipeline

```
  princeton-nlp/SWE-bench_Lite  (HuggingFace, 300 test instances)
              │
              │  bench/dataset.py — load, sort by instance_id, slice
              ▼
  ┌──────────────────────────────────────────────────────────┐
  │  for each instance (bench/runner.py):                    │
  │    git clone <repo> && git checkout <base_commit>        │
  │    agent run  ──►  the SAME orchestrator the UI uses     │
  │    git diff   ──►  model_patch                           │
  │    persist BenchmarkTask + a full Run                    │
  └──────────────────────────┬───────────────────────────────┘
                             ▼
              predictions.jsonl   {instance_id, model_name_or_path, model_patch}
                             │
                             ▼
  python -m swebench.harness.run_evaluation
      --dataset_name princeton-nlp/SWE-bench_Lite
      --split test
      --predictions_path predictions.jsonl
      --max_workers N
      --run_id <run_key>
      --instance_ids <ids...>
                             │
                             ▼
              harness report files  (parsed, never re-derived)
                             │
                             ▼
              BenchmarkTask.benchmark_status  +  batch aggregates
```

## What the agent is given

| Given | Withheld |
|---|---|
| `problem_statement` | `FAIL_TO_PASS` |
| the repository at `base_commit` | `PASS_TO_PASS` |
| a suggested test command (file paths only) | `patch` (the gold patch) |
| | `test_patch` |

The suggested test command names the *test files* implicated by `FAIL_TO_PASS`
— which is ordinary engineering, the same thing a human reads off a CI failure —
but never the test node ids. Two tests enforce this:

- `test_the_agent_is_never_given_the_answer_key` greps everything reaching the
  prompt for the gold patch and every `FAIL_TO_PASS`/`PASS_TO_PASS` node id;
- `test_suggested_test_command_names_files_not_test_ids` asserts the command
  contains the file but not the test name.

## Report parsing

Verified against **swebench 5.0.2** by reading
`swebench.harness.reporting.make_run_report` and
`swebench.harness.grading.get_eval_report` in the installed package.

**Per-instance report** (authoritative — written by the grading code itself):

```
<cwd>/logs/run_evaluation/<run_id>/<model_name_or_path with / → __>/<instance_id>/report.json
```

```json
{
  "astropy__astropy-12907": {
    "patch_is_None": false,
    "patch_exists": true,
    "patch_successfully_applied": true,
    "resolved": true,
    "infra_failure": false
  }
}
```

**Batch summary** (fills gaps, supplies counts):

```
<report_dir>/<model_name_or_path with / → __>.<run_id>.json
```

with `resolved_ids`, `unresolved_ids`, `empty_patch_ids`, `error_ids`,
`infra_failure_ids`, `ambiguous_failure_ids`, `incomplete_ids`, plus counts and
`schema_version`.

The parser also accepts the older `logs/evaluation/` layout referenced in some
documentation, so a harness upgrade or downgrade does not silently produce "no
results". A per-instance report always wins over a summary listing.

### Status mapping

| Harness signal | `benchmark_status` | Counts toward resolve rate |
|---|---|---|
| `resolved: true` | `RESOLVED` | numerator + denominator |
| `resolved: false` | `UNRESOLVED` | denominator |
| `patch_is_None` / `empty_patch_ids` | `EMPTY_PATCH` | neither |
| `infra_failure` / `error_ids` / `ambiguous_failure_ids` / `incomplete_ids` | `ERROR` | neither |
| no report at all | `NOT_EVALUATED` | neither |

Empty patches and infrastructure failures are deliberately excluded from the
denominator. Whether to count them is a real methodological choice; excluding
them keeps the rate a measure of *grading outcomes* rather than of
infrastructure. Because every bucket is stored per task, any other convention
can be computed from the persisted data.

## Two verdicts

| Column | Set by | Meaning |
|---|---|---|
| `BenchmarkTask.agent_status` | the agent | Its own tests went green |
| `BenchmarkTask.benchmark_status` | the harness | The official verdict |

They are stored separately, displayed separately, and only the second is
aggregated:

```
resolve_rate = resolved / (resolved + unresolved) × 100
```

`BenchmarkRun.resolve_rate` returns `None` — never `0.0` — when
`evaluated is False` or nothing has been graded. The UI renders "not evaluated"
for `None`. An ungraded batch and a batch that scored zero are different claims.

## Per-instance images

A generic sandbox image cannot run a real repository's tests: astropy needs
numpy, sympy needs mpmath, Django brings its own harness. The benchmark
publishes one image per instance with the repository and its dependencies
installed, and the evaluator runs in exactly those images.

When the docker sandbox is selected, benchmark tasks therefore run in the
official per-instance image, so the agent's `run_tests` observations mean the
same thing the grader's will:

```
{namespace}/sweb.eval.{arch}.{instance_id}:{tag}
swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest
```

`__` becomes `_1776_` because Docker Hub rejects dunders, and the whole string
is lowercased — taken from `swebench.image_builder.image_spec.ImageSpec.name`
and pinned by `test_instance_image_matches_the_official_naming`.

The image is pulled on first use, which is the one place the sandbox is allowed
to reach the network — the pull happens on the host *before* the container is
created with `--network none`. Each image is multiple GB; budget disk
accordingly.

Controlled by:

```bash
PATCHPILOT_SWEBENCH_INSTANCE_IMAGES=true     # default
PATCHPILOT_SWEBENCH_IMAGE_NAMESPACE=swebench
PATCHPILOT_SWEBENCH_IMAGE_TAG=latest
```

With the local sandbox, or with instance images disabled, the agent can still
explore and patch but cannot run the repository's tests — it will finish
without validation, which materially lowers the resolve rate.

## Requirements

| Requirement | Why | Check |
|---|---|---|
| `pip install -e ".[swebench]"` | the harness and `datasets` | `patchpilot doctor` |
| Docker daemon running | the harness builds a container per instance | `patchpilot doctor` |
| ~120 GB free disk | per the official docs | `patchpilot doctor` |
| 16 GB RAM, 8 cores | per the official docs | — |

`evaluator_preflight()` checks all of these **before** generating any patches,
so a batch does not spend money and then fail to be graded. If preflight fails,
patches are still generated and stored, the batch is marked `NOT EVALUATED`
with the reason, and `patchpilot bench evaluate <id>` can grade it later once
the environment is fixed.

## Reproducibility

Each `BenchmarkRun` persists everything needed to repeat it:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-5",
  "sandbox": "docker",
  "max_iterations": 30,
  "max_tool_calls": 160,
  "max_runtime_s": 1800,
  "max_cost_usd": 5.0,
  "evaluate": true,
  "reproduce": {
    "dataset": "princeton-nlp/SWE-bench_Lite",
    "split": "test",
    "selection": "sorted[0:10]"
  }
}
```

Instances are sorted by `instance_id` before slicing, so `sorted[0:10]` selects
exactly the same ten instances on any machine. Explicit `instance_ids` are
recorded verbatim. The harness `run_id` is stored as `run_key`, so PatchPilot's
rows can always be tied back to the harness's own logs on disk.

`GET /api/benchmarks/{id}/predictions` returns the exact predictions file that
was submitted, so a third party can re-grade it independently.

## Commands

```bash
patchpilot bench list --limit 20            # browse the dataset
patchpilot bench run --limit 10             # generate + evaluate
patchpilot bench run -i astropy__astropy-12907 -i django__django-11099
patchpilot bench run --limit 10 --no-evaluate   # patches only
patchpilot bench show <benchmark-id>
patchpilot bench evaluate <benchmark-id>    # grade an existing batch
patchpilot bench resume-bullet              # resume line from real numbers
```

`resume-bullet` reads only officially-evaluated batches and prints nothing at
all if none exist.
