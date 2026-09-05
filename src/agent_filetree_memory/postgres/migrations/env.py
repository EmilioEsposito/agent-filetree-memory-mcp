"""Package-only Alembic environment for the explicit migration runner.

Hosts with their own Alembic environment continue to use configure_host_alembic.
No credentials are read here, and no engine or PostgreSQL schema is created.
"""

from alembic import context

from agent_filetree_memory.postgres.migrations import schema_from_config

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("use the migration runner with an explicit database connection")

context.configure(
    connection=connection,
    version_table_schema=schema_from_config(config),
)
with context.begin_transaction():
    context.run_migrations()
