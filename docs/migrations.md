# Database migrations and upgrades

The package owns its tables and ships the migrations that evolve them. The host
owns the database, credentials, schema provisioning, migration execution, and
application rollout. Installing a new package version does not modify a database.
Neither the MCP server nor the management API runs migrations at startup.

Use a dedicated PostgreSQL schema (default `agent_filetree_memory`). Choose one
of the following migration environments and keep using its version table. Do not
switch runners or stamp a database to work around an unrecognized revision.

## Standalone deployments

Install `agent-filetree-memory-mcp[postgres]` (or `[all]`). Provision the selected
schema with your normal database administration process, and supply `DATABASE_URL`
through your secret manager or environment. No memory encryption or signing keys
are needed for schema migrations.

```sh
agent-filetree-memory-migrate check
agent-filetree-memory-migrate upgrade
agent-filetree-memory-migrate check
```

Both commands accept `--schema`, defaulting to
`AGENT_FILETREE_MEMORY_DATABASE_SCHEMA` or `agent_filetree_memory`. `upgrade`
also accepts `--constraint-namespace` (default `afm`); keep this setting stable
across upgrades. Commands print a JSON status with `current_revisions`,
`required_revision`, and `is_current`. `check` exits 1 when the database is behind,
uninitialized, or ahead of the installed package; it performs no DDL. It checks
recorded revisions, not manual schema drift.

The standalone runner stores revisions in `<selected_schema>.alembic_version`.
It applies the packaged `agent_filetree_memory@head` in one transaction, and a
transaction-scoped advisory lock rejects overlapping standalone migrations of the
same schema. Retrying after the other migration commits is safe. Failures roll
back both transactional DDL and revision changes. Unknown revisions cause an
error before migration DDL, including histories belonging to another application
or a newer package. Schema creation, downgrade, and version stamping are not
exposed by this command.

Embedded hosts can use the same runner with a SQLAlchemy connection:

```python
from agent_filetree_memory.postgres.migrations.runner import upgrade_schema

async with engine.begin() as connection:
    await connection.run_sync(upgrade_schema, schema="agent_filetree_memory")
```

The synchronous function requires a caller-owned transaction. Existing hosts
with a shared Alembic history should use the integration below instead.

## Hosts that already use Alembic

Keep the host's `env.py`, credentials, and version table. Register the installed
package's revisions before Alembic constructs its script directory:

```python
from alembic import command
from alembic.config import Config
from agent_filetree_memory.postgres.migrations import configure_host_alembic

config = Config("alembic.ini")
configure_host_alembic(config, schema="agent_filetree_memory")
command.upgrade(config, "agent_filetree_memory@head")
```

The helper preserves the host's existing version locations and adds the package
location. It must run **before `command.upgrade`**, not only inside `env.py`.
The host environment supplies its connection and calls `context.run_migrations()`
inside its normal transaction. It may keep `alembic_version` in its existing
location; the package does not relocate it.

With multiple independent branches, use a branch-qualified head as above, or
`heads` to apply all registered branches. Plain `head` can be ambiguous. If a
host migration needs a package table, declare `depends_on` for the required
package revision. The host must serialize its own migration jobs; the
standalone runner's advisory lock does not coordinate an arbitrary host runner.

Do not copy package revisions into the hosting repository or generate replacement
migrations for package tables. Host migrations cover host-owned tables and
host-specific data transformations. For host autogeneration with only host
metadata, exclude package tables from reflection so Alembic does not propose
dropping them. For example, when the package has a dedicated schema, the host's
`include_object` filter can omit tables where `obj.schema` is the package schema.
`migration_metadata(schema)` exposes current package metadata for explicit drift
checks or combined metadata comparisons; it is not a request to author host
migrations for this package.

A host can check the package revision using its actual version-table location:

```python
from agent_filetree_memory.postgres.migrations.runner import schema_status

async with engine.connect() as connection:
    status = await connection.run_sync(
        schema_status,
        schema="agent_filetree_memory",
        version_table="alembic_version",
        version_table_schema="public",  # Match the host's Alembic configuration.
    )
    if not status.is_current:
        raise RuntimeError("Apply the installed memory package's migrations before serving")
```

Other host branch heads may coexist with the required package head. This check
is intentionally explicit so hosts can place it in deployment validation or
readiness checks without adding a query to every memory request.

## Release and rollout contract

1. Pin the chosen package version in the hosting service.
2. Review its upgrade notes and test upgrades on a disposable database containing
   representative old-version data. Keep an appropriate backup before production
   schema changes.
3. Apply the shipped migrations once using the established environment and
   credentials authorized for DDL.
4. Verify the package revision and roll out compatible service code. For rolling
   deployments, schema changes must remain compatible with the old workers until
   they drain; use expand/contract changes for destructive transitions.

Revisions `afm_0003`–`afm_0005` now contain their historical table definitions,
including legacy-table adoption checks. This corrects their dependence on live
application metadata without changing their intended DDL or revision IDs.
Existing databases at `afm_0005` need no new schema change for this correction.
Fresh and incremental installs produce the same intended schema. Policy
migrations continue to preserve existing grants without inferring new access.

For future schema changes, add a new revision with frozen DDL and any required
package-owned data transition. Never import evolving model definitions into a
historical revision. Test fresh installation, upgrade from the preceding
revision with existing data, metadata consistency, and any supported downgrade.
Do not downgrade a database simply because application code is rolled back:
removing package-created tables can delete data. Follow a tested rollback or
forward-fix plan for that release.
