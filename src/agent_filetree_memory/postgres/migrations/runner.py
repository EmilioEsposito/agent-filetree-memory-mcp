"""Explicit upgrades and read-only revision checks on host-supplied connections."""

from dataclasses import dataclass

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect, text

from ...domain.errors import ConfigurationError
from ..schema import DEFAULT_SCHEMA, validate_schema_name
from . import configure_host_alembic, package_version_location


def _config(schema: str, constraint_namespace: str = "afm") -> Config:
    config = Config()
    config.set_main_option("script_location", str(package_version_location().parent))
    return configure_host_alembic(
        config, schema=schema, constraint_namespace=constraint_namespace
    )


@dataclass(frozen=True)
class SchemaStatus:
    schema: str
    current_revisions: tuple[str, ...]
    required_revision: str

    @property
    def is_current(self) -> bool:
        """Other host branch heads may coexist with the required package head."""
        return self.required_revision in self.current_revisions


def schema_status(
    connection: Connection,
    *,
    schema: str = DEFAULT_SCHEMA,
    version_table: str = "alembic_version",
    version_table_schema: str | None = None,
) -> SchemaStatus:
    """Check recorded revisions without DDL; this is not a schema-drift audit.

    Integrated hosts must pass their actual Alembic version-table location.
    By default it lives in the package's selected PostgreSQL schema.
    """
    schema = validate_schema_name(schema)
    scripts = ScriptDirectory.from_config(_config(schema))
    required = scripts.get_revision("agent_filetree_memory@head").revision
    context = MigrationContext.configure(
        connection,
        opts={
            "version_table": validate_schema_name(version_table),
            "version_table_schema": validate_schema_name(
                version_table_schema or schema
            ),
        },
    )
    return SchemaStatus(schema, tuple(sorted(context.get_current_heads())), required)


def upgrade_schema(
    connection: Connection,
    *,
    schema: str = DEFAULT_SCHEMA,
    constraint_namespace: str = "afm",
) -> SchemaStatus:
    """Apply the installed package's branch in the caller's transaction.

    For standalone installs only: uses schema.alembic_version. Existing hosts
    that share a revision table with application migrations must use their own
    Alembic environment. Never stamps, creates a schema, or migrates on startup.
    """
    config = _config(schema, constraint_namespace)
    if not connection.in_transaction():
        raise ConfigurationError("upgrade_schema requires an explicit transaction")
    if not inspect(connection).has_schema(schema):
        raise ConfigurationError(
            "create the selected PostgreSQL schema before migrating"
        )
    locked = connection.execute(
        text(
            "SELECT pg_try_advisory_xact_lock(hashtext(:schema), hashtext(:operation))"
        ),
        {"schema": schema, "operation": "agent_filetree_memory:migrations"},
    ).scalar_one()
    if not locked:
        raise ConfigurationError(
            "another package migration is running for this schema; retry after it completes"
        )
    scripts = ScriptDirectory.from_config(config)
    known = {revision.revision for revision in scripts.walk_revisions()}
    status = schema_status(connection, schema=schema)
    if set(status.current_revisions) - known:
        raise ConfigurationError(
            "version table contains unknown revisions; use the host Alembic environment "
            "or the matching package version"
        )
    config.attributes["connection"] = connection
    command.upgrade(config, "agent_filetree_memory@head")
    return schema_status(connection, schema=schema)
