# ADR-0001 — Persist events in SQL and stream with an SSE cursor

**Status:** accepted

## Context

The UI must follow a live run and also replay a finished one. The obvious
choice is Redis pub/sub with an in-memory buffer, but that ties the API process
that *served* the subscription to the process that *ran* the job, and it loses
everything on restart.

## Decision

Events are rows in an append-only `events` table with a monotonic per-run
`seq`. The SSE endpoint polls `seq > cursor` and streams whatever it finds.

## Consequences

- A reconnecting browser passes `?after=<last seq>` and resumes exactly, with
  no gap and no duplicate.
- A finished run replays through the identical code path, so the live view and
  the historical view cannot drift apart.
- The API process need not be the process that executed the run — worker and
  API are already decoupled by the queue.
- No broker to install or operate; `pip install` is the whole setup.
- Cost: up to 500 ms of latency and one indexed query per poll. Irrelevant next
  to a multi-minute agent run.
- If event volume ever justified it, a Redis fan-out could sit *in front* of
  this without changing the contract, because the durable log stays the truth.
