"""A real encrypted store and MCP adapter, isolated in a new schema per case."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import base64
import os
from uuid import uuid4

from sqlalchemy import text

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.crypto import EnvelopeEncryptor, LocalKeyringDekProvider
from agent_filetree_memory.domain.models import MemoryAction, Scope, VerifiedInvocation
from agent_filetree_memory.mcp import create_mcp_server
from agent_filetree_memory.postgres import (
    PostgresMemoryStore,
    PostgresRuntime,
    PostgresStoreConfig,
)
from agent_filetree_memory.postgres.migrations.runner import upgrade_schema


@asynccontextmanager
async def environment(files):
    url = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "use python -m devtools.postgres -- uv run --group evals python -m evals.run ..."
        )
    schema = "afm_eval_" + uuid4().hex[:16]
    runtime = PostgresRuntime.from_url(url, schema=schema)
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
            await connection.run_sync(upgrade_schema, schema=schema)
        encryptor = EnvelopeEncryptor(
            LocalKeyringDekProvider(
                {"eval": base64.b64encode(os.urandom(32)).decode()},
                active_key_id="eval",
            )
        )
        store = PostgresMemoryStore(
            runtime,
            encryptor,
            config=PostgresStoreConfig(
                idempotency_index_key=os.urandom(32), rate_limit_operations=10000
            ),
        )
        service = MemoryService(store)
        now = datetime.now(timezone.utc)
        invocation = VerifiedInvocation(
            scope=Scope("eval-workspace", "eval-agent"),
            principal_id="eval-principal",
            invocation_id="eval-invocation",
            capability_id="eval-capability",
            issuer="eval",
            audience="eval",
            allowed_actions=frozenset(MemoryAction),
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
        for index, (path, content) in enumerate(files.items()):
            await service.write(
                invocation, path, content, idempotency_key=f"seed-{index}"
            )

        async def resolver(ctx, action):
            invocation.require(action)
            return invocation

        yield create_mcp_server(service, resolver), service, invocation
    finally:
        try:
            async with runtime.engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        finally:
            await runtime.close()
