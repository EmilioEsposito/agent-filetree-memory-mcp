# Alembic integration

This directory contains package-owned, versioned Alembic migrations. The host
owns the database, credentials, PostgreSQL schema creation, and when migrations
run. Migration definitions ship with the package; hosts do not recreate them.

Standalone installations can run `agent-filetree-memory-migrate upgrade` and
`agent-filetree-memory-migrate check`, using `DATABASE_URL` and an existing
schema. This environment tracks revisions in the selected schema's
`alembic_version` table and rejects unknown histories.

Hosts with an existing Alembic environment call
`configure_host_alembic(config, schema="your_schema")` before invoking Alembic
commands, then upgrade to `agent_filetree_memory@head`. The helper appends the
installed package version location and preserves the host's implicit
`<script_location>/versions` directory. Keep the host's existing version table
and serialize its migration jobs.

`migration_metadata("your_schema")` supplies current metadata for drift checks.
Do not autogenerate host-owned replacements for packaged revisions. Historical
revisions use frozen DDL; they must never import evolving application models.

The runner accepts an explicit connection and transaction. It never creates a
PostgreSQL schema or runs at server startup. See `docs/migrations.md` in the
source distribution or repository for complete standalone and host examples.
