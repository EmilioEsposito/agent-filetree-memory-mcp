from __future__ import annotations

import base64
from collections.abc import AsyncIterator
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_filetree_memory.crypto import EnvelopeEncryptor, LocalKeyringDekProvider
from agent_filetree_memory.postgres import (
    PostgresMemoryStore,
    PostgresRuntime,
    PostgresStoreConfig,
)
from agent_filetree_memory.postgres.migrations.runner import upgrade_schema

TEST_INDEX_KEY = b"test-only-idempotency-index-key-32"


def _postgres_url() -> str:
    value = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "set AGENT_FILETREE_MEMORY_TEST_DATABASE_URL to a disposable PostgreSQL database"
        )
    return value


@pytest.fixture
async def postgres_runtime() -> AsyncIterator[PostgresRuntime]:
    schema = "afm_test_" + uuid4().hex[:16]
    runtime = PostgresRuntime.from_url(_postgres_url(), schema=schema)
    assert runtime.engine is not None
    async with runtime.engine.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
        await connection.run_sync(upgrade_schema, schema=schema)
    try:
        yield runtime
    finally:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await runtime.close()


@pytest.fixture
def encryptor() -> EnvelopeEncryptor:
    encoded_key = base64.b64encode(os.urandom(32)).decode("ascii")
    return EnvelopeEncryptor(
        LocalKeyringDekProvider({"test-key": encoded_key}, active_key_id="test-key")
    )


@pytest.fixture
def postgres_store(postgres_runtime, encryptor) -> PostgresMemoryStore:
    return PostgresMemoryStore(
        postgres_runtime,
        encryptor,
        config=PostgresStoreConfig(idempotency_index_key=TEST_INDEX_KEY),
    )
