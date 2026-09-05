import asyncio
import json
import os
import sys
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text

from agent_filetree_memory.domain.errors import ConfigurationError
from agent_filetree_memory.postgres import PostgresRuntime
from agent_filetree_memory.postgres.migrations import (
    configure_host_alembic,
    package_version_location,
)
from agent_filetree_memory.postgres.migrations.runner import (
    schema_status,
    upgrade_schema,
)

pytestmark = pytest.mark.live


@pytest.fixture
async def empty_runtime():
    url = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable PostgreSQL database")
    runtime = PostgresRuntime.from_url(url, schema="afm_runner_" + uuid4().hex[:16])
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{runtime.schema}"'))
        yield runtime
    finally:
        try:
            async with runtime.engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA "{runtime.schema}" CASCADE')
                )
        finally:
            await runtime.close()


async def test_check_is_read_only_and_upgrade_is_repeatable(empty_runtime):
    runtime = empty_runtime
    async with runtime.engine.begin() as connection:
        status = await connection.run_sync(schema_status, schema=runtime.schema)
        assert not status.is_current and not status.current_revisions
        assert (
            await connection.run_sync(
                lambda c: inspect(c).get_table_names(schema=runtime.schema)
            )
            == []
        )
        upgraded = await connection.run_sync(upgrade_schema, schema=runtime.schema)
        assert upgraded.is_current and upgraded.required_revision == "afm_0005"
        assert (
            await connection.run_sync(upgrade_schema, schema=runtime.schema) == upgraded
        )


async def test_historical_revisions_ignore_evolving_models(empty_runtime, monkeypatch):
    from agent_filetree_memory.control_plane import namespace_store

    def unavailable_models(*args, **kwargs):
        raise AssertionError("historical migration accessed current application models")

    monkeypatch.setattr(
        namespace_store, "namespace_tables_for_schema", unavailable_models
    )
    runtime = empty_runtime
    async with runtime.engine.begin() as connection:
        status = await connection.run_sync(
            upgrade_schema, schema=runtime.schema, constraint_namespace="custom"
        )
        assert status.is_current
        constraints = await connection.run_sync(
            lambda c: inspect(c).get_check_constraints(
                "workspace_members", schema=runtime.schema
            )
        )
        assert "ck_custom_workspace_members_role" in {c["name"] for c in constraints}


async def test_incremental_upgrade_preserves_existing_data(empty_runtime):
    runtime = empty_runtime

    def install_old(connection):
        config = Config()
        config.set_main_option(
            "script_location", str(package_version_location().parent)
        )
        configure_host_alembic(config, schema=runtime.schema)
        config.attributes["connection"] = connection
        command.upgrade(config, "afm_0003")

    async with runtime.engine.begin() as connection:
        await connection.run_sync(install_old)
        await connection.execute(
            text(
                f'INSERT INTO "{runtime.schema}".workspaces '
                "(workspace_id, slug, created_by_principal_id, integrity_version, integrity_tag) "
                "VALUES ('existing', 'existing', 'owner', 1, decode(repeat('ab', 32), 'hex'))"
            )
        )
    async with runtime.engine.begin() as connection:
        assert not (
            await connection.run_sync(schema_status, schema=runtime.schema)
        ).is_current
        assert (
            await connection.run_sync(upgrade_schema, schema=runtime.schema)
        ).is_current
        assert (
            await connection.execute(
                text(f'SELECT workspace_id FROM "{runtime.schema}".workspaces')
            )
        ).scalar_one() == "existing"
        assert (
            await connection.execute(
                text(f'SELECT count(*) FROM "{runtime.schema}".agent_access_policies')
            )
        ).scalar_one() == 0


@pytest.mark.parametrize("revision", ["host_0001", "afm_9999"])
async def test_unknown_or_newer_history_is_never_rewritten(empty_runtime, revision):
    runtime = empty_runtime
    async with runtime.engine.begin() as connection:
        await connection.execute(
            text(
                f'CREATE TABLE "{runtime.schema}".alembic_version (version_num varchar(32) PRIMARY KEY)'
            )
        )
        await connection.execute(
            text(f'INSERT INTO "{runtime.schema}".alembic_version VALUES (:revision)'),
            {"revision": revision},
        )
    async with runtime.engine.begin() as connection:
        with pytest.raises(ConfigurationError, match="unknown revisions"):
            await connection.run_sync(upgrade_schema, schema=runtime.schema)
        assert not (
            await connection.run_sync(schema_status, schema=runtime.schema)
        ).is_current
        assert await connection.run_sync(
            lambda c: inspect(c).get_table_names(schema=runtime.schema)
        ) == ["alembic_version"]


async def test_failed_upgrade_rolls_back_ddl_and_version_table(empty_runtime):
    runtime = empty_runtime
    # A partial legacy control plane causes afm_0003 to reject adoption after
    # afm_0001/2 have run. Their DDL and revision writes must roll back too.
    async with runtime.engine.begin() as connection:
        await connection.execute(
            text(f'CREATE TABLE "{runtime.schema}".workspaces (sentinel text)')
        )
    with pytest.raises(RuntimeError, match="partial control-plane"):
        async with runtime.engine.begin() as connection:
            await connection.run_sync(upgrade_schema, schema=runtime.schema)
    async with runtime.engine.connect() as connection:
        assert await connection.run_sync(
            lambda c: inspect(c).get_table_names(schema=runtime.schema)
        ) == ["workspaces"]


async def test_concurrent_runner_fails_until_transaction_releases_lock(empty_runtime):
    runtime = empty_runtime
    async with runtime.engine.begin() as first:
        await first.run_sync(upgrade_schema, schema=runtime.schema)
        async with runtime.engine.begin() as second:
            with pytest.raises(ConfigurationError, match="another package migration"):
                await second.run_sync(upgrade_schema, schema=runtime.schema)
    async with runtime.engine.begin() as connection:
        assert (
            await connection.run_sync(upgrade_schema, schema=runtime.schema)
        ).is_current


async def test_missing_schema_is_not_created(empty_runtime):
    missing = empty_runtime.schema + "_missing"
    async with empty_runtime.engine.begin() as connection:
        assert not (await connection.run_sync(schema_status, schema=missing)).is_current
        with pytest.raises(ConfigurationError, match="create the selected"):
            await connection.run_sync(upgrade_schema, schema=missing)
        assert not await connection.run_sync(lambda c: inspect(c).has_schema(missing))


async def test_cli_check_upgrade_and_check_from_separate_process(empty_runtime):
    async def invoke(action):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_filetree_memory.migrate_cli",
            action,
            "--schema",
            empty_runtime.schema,
            env={
                **os.environ,
                "DATABASE_URL": os.environ["AGENT_FILETREE_MEMORY_TEST_DATABASE_URL"],
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert not stderr, stderr.decode()
        return process.returncode, json.loads(stdout)

    code, status = await invoke("check")
    assert code == 1 and not status["is_current"]
    code, status = await invoke("upgrade")
    assert code == 0 and status["is_current"]
    code, status = await invoke("check")
    assert code == 0 and status["is_current"]
