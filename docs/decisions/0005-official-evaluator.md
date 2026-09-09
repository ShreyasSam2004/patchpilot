# ADR-0005 — The official SWE-bench harness is the only source of a resolve rate

**Status:** accepted

## Context

PatchPilot already runs tests. It would be easy — and much faster — to apply an
instance's `test_patch`, run `FAIL_TO_PASS`, and call the result a SWE-bench
score. Every detail of that pipeline (container image, dependency pinning, test
selection, log parsing) is a place to be subtly wrong, and the errors tend to
be flattering.

## Decision

Shell out to `python -m swebench.harness.run_evaluation` and parse the
harness's own report files. Never re-derive a verdict.

Store the agent's own conclusion (`agent_status`) and the harness's verdict
(`benchmark_status`) in separate columns. Only the latter is aggregated.
`resolve_rate` returns `None`, not `0.0`, until the harness has graded a batch.

## Consequences

- A number reported here means the same thing as everyone else's number.
- Evaluation requires Docker and ~120 GB of disk, which preflight checks
  *before* any money is spent on generation.
- When the harness cannot run, patches are still produced and the batch is
  marked `NOT EVALUATED` — visibly, with the reason — and can be graded later
  with `patchpilot bench evaluate`.
- Report parsing is coupled to the harness's output layout. Mitigated by
  pinning the parser to the installed version's actual behaviour (read from
  `swebench.harness.reporting` and `swebench.harness.grading`), accepting both
  known directory layouts, and testing against the exact schema.
