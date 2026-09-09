# ADR-0006 — The local sandbox maps a bare `python` onto `sys.executable`

**Status:** accepted

## Context

`python -m pytest` is the most common test command there is. In the local
sandbox, `python` resolves against `PATH` — which is frequently a *different*
interpreter from the one PatchPilot runs under, because executing
`.venv/bin/python` does not put `.venv/bin` on `PATH`.

The symptom is `No module named pytest`. The agent cannot validate anything,
cannot legitimately finish, and burns its entire iteration budget. This was
found by an end-to-end test, not by reasoning about it.

## Decision

`LocalSandbox.normalize_argv()` rewrites a **bare** `python`, `python3` or `py`
to `sys.executable`, and a bare `pytest` that is not on `PATH` to
`sys.executable -m pytest`. An explicit path is never rewritten.

`DockerSandbox` deliberately does not do this: the container has its own
Python, and a host path would be meaningless inside it.

## Consequences

- `python -m pytest` works out of the box, which is the least surprising
  behaviour for a tool whose job is running a project's tests.
- A user who needs a different interpreter passes an explicit path, which is
  respected.
- The rewrite lives in the sandbox layer, where "which environment am I"
  is already the responsibility, rather than being smeared across the tools.
- Cost: one deliberate behavioural difference between the two sandboxes,
  documented here and covered by tests.
