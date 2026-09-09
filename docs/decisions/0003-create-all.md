# ADR-0003 — `create_all` for schema, kept Alembic-ready

**Status:** accepted

## Context

The project needs a schema. Alembic is correct for a deployed service, but adds
a migration chain, an async `env.py`, and a step to every model change —
friction that is hard to justify for a single-developer portfolio project with
no production data.

## Decision

`Base.metadata.create_all()` at startup. The models deliberately avoid
dialect-specific column types (JSON via `sa.JSON`, timestamps set in Python),
so `alembic revision --autogenerate` can be pointed at the same metadata
whenever migrations become necessary.

## Consequences

- Zero-friction setup: `patchpilot serve` creates the schema.
- The same models work on SQLite and PostgreSQL; only `DATABASE_URL` changes.
- **Schema changes are not migrations.** Altering a column requires recreating
  the database. This is documented as a limitation in the README.
- Adding Alembic later is additive: generate an initial revision from the
  existing metadata and stamp any deployed database.
