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
from agent_filetree_memory.control_plane.namespace_store import (
    namespace_tables_for_schema,
)

pytestmark = pytest.mark.live


def _database_url() -> str:
    value = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "set AGENT_FILETREE_MEMORY_TEST_DATABASE_URL to a disposable PostgreSQL database"
        )
    return value


def _host_config(tmp_path, *, url: str, schema: str) -> Config:
    host_migrations = tmp_path / "host_migrations"
    host_migrations.mkdir()
    (host_migrations / "versions").mkdir()
    (host_migrations / "env.py").write_text(
        """
import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool
from agent_filetree_memory.postgres.migrations import migration_metadata

config = context.config
schema = config.attributes["agent_filetree_memory_schema"]

def configure(connection):
    def include_object(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            return obj.schema == schema
        return True

    context.configure(
        connection=connection,
        target_metadata=migration_metadata(schema),
        include_schemas=True,
        include_object=include_object,
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

    config = Config()
    config.set_main_option("script_location", str(host_migrations))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_packaged_revision_upgrades_and_downgrades_from_host_alembic(tmp_path):
    url = _database_url()
    schema = "afm_migration_" + uuid4().hex[:12]
    config = _host_config(tmp_path, url=url, schema=schema)

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

    async def reflected_schema() -> tuple[
        dict[str, set[str]], dict[str, set[str]]
    ]:
        engine = create_async_engine(url)
        async with engine.connect() as connection:
            result = await connection.run_sync(
                lambda sync_connection: (
                    {
                        table.name: {
                            column["name"]
                            for column in inspect(sync_connection).get_columns(
                                table.name, schema=schema
                            )
                        }
                        for table in migration_metadata(schema).tables.values()
                    },
                    {
                        table.name: {
                            constraint["name"]
                            for constraint in (
                                inspect(sync_connection).get_check_constraints(
                                    table.name, schema=schema
                                )
                                + inspect(sync_connection).get_unique_constraints(
                                    table.name, schema=schema
                                )
                            )
                            if constraint["name"] is not None
                        }
                        for table in migration_metadata(schema).tables.values()
                    },
                )
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
        configure_host_alembic(config, schema=schema)
        command.upgrade(config, "agent_filetree_memory@head")
        names = asyncio.run(table_names())
        assert names == {
            "_afm_control_plane_installation",
            "agent_grants",
            "agent_managers",
            "agent_profiles",
            "alembic_version",
            "management_audit_events",
            "memory_audit_events",
            "memory_idempotency",
            "memory_objects",
            "memory_quotas",
            "memory_rate_buckets",
            "memory_versions",
            "principal_profiles",
            "workspace_invitations",
            "workspace_members",
            "workspaces",
        }
        expected_columns = {
            table.name: {column.name for column in table.columns}
            for table in migration_metadata(schema).tables.values()
        }
        reflected, constraint_names = asyncio.run(reflected_schema())
        assert reflected
        assert reflected == expected_columns
        assert "principal_id" in reflected["memory_audit_events"]
        assert "integrity_tag" in reflected["workspace_members"]
        assert "ck_memory_objects_object_kind" in constraint_names[
            "memory_objects"
        ]
        assert "ck_memory_audit_events_outcome" in constraint_names[
            "memory_audit_events"
        ]
        assert any(
            name.startswith(
                "uq_memory_idempotency_workspace_id_agent_profile_id_loo_"
            )
            for name in constraint_names["memory_idempotency"]
        )
        command.check(config)
        command.downgrade(config, "agent_filetree_memory@base")
        assert asyncio.run(table_names()) == {"alembic_version"}
    finally:
        asyncio.run(cleanup())


def test_control_plane_migration_adopts_and_preserves_compatible_host_tables(
    tmp_path,
):
    url = _database_url()
    schema = "afm_adopt_" + uuid4().hex[:12]
    constraint_namespace = "host_memory"
    config = _host_config(tmp_path, url=url, schema=schema)
    expected_tables = {
        table.name
        for table in namespace_tables_for_schema(
            schema,
            constraint_namespace=constraint_namespace,
        ).metadata.tables.values()
    }

    async def prepare() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            await connection.run_sync(
                namespace_tables_for_schema(
                    schema,
                    constraint_namespace=constraint_namespace,
                ).metadata.create_all
            )
        await engine.dispose()

    async def state() -> tuple[set[str], str | None]:
        engine = create_async_engine(url)
        async with engine.connect() as connection:
            names = set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = :schema"
                        ),
                        {"schema": schema},
                    )
                ).scalars()
            )
            ownership = None
            if "_afm_control_plane_installation" in names:
                ownership = (
                    await connection.execute(
                        text(
                            f'SELECT ownership FROM "{schema}".'
                            '"_afm_control_plane_installation" '
                            "WHERE revision = 'afm_0003'"
                        )
                    )
                ).scalar_one()
        await engine.dispose()
        return names, ownership

    async def cleanup() -> None:
        engine = create_async_engine(url)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await engine.dispose()

    asyncio.run(prepare())
    try:
        configure_host_alembic(
            config,
            schema=schema,
            constraint_namespace=constraint_namespace,
        )
        command.stamp(config, "afm_0002")
        command.upgrade(config, "agent_filetree_memory@head")
        names, ownership = asyncio.run(state())
        assert names == expected_tables | {
            "_afm_control_plane_installation",
            "alembic_version",
        }
        assert ownership == "adopted"

        command.downgrade(config, "afm_0002")
        names, ownership = asyncio.run(state())
        assert names == expected_tables | {"alembic_version"}
        assert ownership is None
    finally:
        asyncio.run(cleanup())
