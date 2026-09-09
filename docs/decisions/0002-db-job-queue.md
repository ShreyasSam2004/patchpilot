# ADR-0002 — A database-backed job queue instead of Celery/RQ

**Status:** accepted

## Context

Agent runs take minutes, so they cannot execute inside an HTTP request. The
conventional answer is Celery or RQ plus Redis.

## Decision

A run row *is* the job. A worker claims one with a conditional UPDATE:

```sql
UPDATE runs SET state='PREPARING', worker_id=:me
WHERE id=:id AND state='PENDING'
```

Whoever's UPDATE reports `rowcount == 1` owns the job.

## Consequences

- Atomic on both SQLite and PostgreSQL with no advisory locks and no
  `SKIP LOCKED`, so the same code path runs in development and production.
- One fewer service to install, configure and secure.
- Job state is queryable with the same tools as everything else, and a job
  cannot exist without its run row.
- Crash recovery is a timestamp comparison: `requeue_stale_runs()` returns runs
  whose heartbeat is older than 15 minutes.
- Cancellation is a flag the worker checks on each heartbeat.
- Cost: polling latency, and a claim-throughput ceiling far above what a system
  running minute-long jobs will ever reach.
- `test_queue_claims_a_run_exactly_once` races two workers for one job and
  asserts exactly one wins.
