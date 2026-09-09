# ADR-0004 — Ship two sandboxes and never silently substitute one

**Status:** accepted

## Context

Running untrusted repository tests demands container isolation. But requiring a
Docker daemon to run a three-second fixture makes the fast feedback loop slow,
and makes the project undemoable on machines without Docker — including, as it
happens, the machine this was built on.

## Decision

Two implementations of one `Sandbox` protocol:

- `DockerSandbox` — container per run, `--network none`, cpu/memory/pid limits,
  capabilities dropped. The correct posture for untrusted code.
- `LocalSandbox` — subprocess with a working-directory jail, environment
  allow-list, timeouts, process-tree kill and output caps.

`create_sandbox(allow_fallback=False)` is the default. Degrading from Docker to
local requires an explicit opt-in, and the run record stores which sandbox
actually ran.

## Consequences

- Fixtures and tests run in seconds with no daemon.
- Untrusted repositories get real isolation when Docker is available.
- The weaker option never *pretends*: `LocalSandbox.info()` returns warnings
  that surface on the landing page, in `/api/status`, and in Settings.
- A benchmark run cannot quietly lose its isolation guarantees, because
  fallback is off unless requested.
- Cost: two implementations to maintain, and one deliberate behavioural
  difference — `LocalSandbox` maps a bare `python` onto `sys.executable`
  (ADR-0006), which `DockerSandbox` must not do because the container has its
  own Python.
