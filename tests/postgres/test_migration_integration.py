from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_filetree_memory.postgres.migrations import (
    configure_host_alembic,
    migration_metadata,
)

pytestmark = pytest.mark.live


def _database_url() -> str:
    value = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "set AGENT_FILETREE_MEMORY_TEST_DATABASE_URL to a disposable PostgreSQL database"
        )
    return value


def test_packaged_revision_upgrades_and_downgrades_from_host_alembic(tmp_path):
    url = _database_url()
    schema = "afm_migration_" + uuid4().hex[:12]
    host_migrations = tmp_path / "host_migrations"
    host_migrations.mkdir()
    (host_migrations / "versions").mkdir()
    (host_migrations / "env.py").write_text(
        """
import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

config = context.config
schema = config.attributes["agent_filetree_memory_schema"]

def configure(connection):
    context.configure(
        connection=connection,
        include_schemas=True,
        version_table_schema=schema,
    )
    with context.begin_transaction():
        context.run_migrations()

async def online():
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(configure)
    await engine.dispose()

asyncio.run(online())
""".strip()
        + "\n",
        encoding="utf-8",
    )

    async def prepare() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        await engine.dispose()

    async def table_names() -> set[str]:
        engine = create_async_engine(url)
        async with engine.connect() as connection:
            names = (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :schema"
                    ),
                    {"schema": schema},
                )
            ).scalars().all()
        await engine.dispose()
        return set(names)

    async def reflected_columns() -> dict[str, set[str]]:
        engine = create_async_engine(url)
        async with engine.connect() as connection:
            result = await connection.run_sync(
                lambda sync_connection: {
                    table.name: {
                        column["name"]
                        for column in inspect(sync_connection).get_columns(
                            table.name, schema=schema
                        )
                    }
                    for table in migration_metadata(schema).tables.values()
                }
            )
        await engine.dispose()
        return result

    async def cleanup() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await engine.dispose()

    asyncio.run(prepare())
    try:
        config = Config()
        config.set_main_option("script_location", str(host_migrations))
        config.set_main_option("sqlalchemy.url", url)
        configure_host_alembic(config, schema=schema)
        command.upgrade(config, "agent_filetree_memory@head")
        names = asyncio.run(table_names())
        assert names == {
            "alembic_version",
            "memory_audit_events",
            "memory_idempotency",
            "memory_objects",
            "memory_quotas",
            "memory_rate_buckets",
            "memory_versions",
        }
        expected_columns = {
            table.name: {column.name for column in table.columns}
            for table in migration_metadata(schema).tables.values()
        }
        reflected = asyncio.run(reflected_columns())
        assert reflected
        assert reflected == expected_columns
        assert "principal_id" in reflected["memory_audit_events"]
        command.downgrade(config, "agent_filetree_memory@base")
        assert asyncio.run(table_names()) == {"alembic_version"}
    finally:
        asyncio.run(cleanup())
