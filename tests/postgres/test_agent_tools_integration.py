import asyncio
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp import Client

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.domain.errors import (
    EditConflict,
    IdempotencyConflict,
    QuotaExceeded,
    VersionConflict,
)
from agent_filetree_memory.domain.models import MemoryAction, Scope, VerifiedInvocation
from agent_filetree_memory.mcp import create_mcp_server

pytestmark = pytest.mark.live


async def test_packaged_adapter_over_stdio_in_disposable_sandbox():
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "devtools.serve"],
        env={
            "AGENT_FILETREE_MEMORY_TEST_DATABASE_URL": os.environ[
                "AGENT_FILETREE_MEMORY_TEST_DATABASE_URL"
            ]
        },
    )
    async with Client(transport, timeout=20) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"memory_glob", "memory_grep", "memory_edit"} <= names
        result = await client.call_tool("memory_read", {"path": "/ops/service.md"})
        assert "Retry limit: 3" in result.structured_content["content"]
        assert result.structured_content["version"] == 1


def invocation():
    now = datetime.now(timezone.utc)
    return VerifiedInvocation(
        Scope("tools", "agent"),
        "principal",
        "invocation",
        "capability",
        "issuer",
        "audience",
        frozenset(MemoryAction),
        now,
        now + timedelta(hours=1),
    )


async def test_edit_atomic_retries_and_history(postgres_store):
    service, inv = MemoryService(postgres_store), invocation()
    await service.write(
        inv, "/notes.md", "# A\nvalue: 1\n\n# B\nvalue: 1\n", idempotency_key="seed"
    )
    args = dict(expected_version=1, idempotency_key="edit-1", change_comment="Update B")
    with pytest.raises(EditConflict):
        await service.edit(inv, "/notes.md", "value: 1", "value: 2", **args)
    assert (await service.read(inv, "/notes.md")).version == 1
    result = await service.edit(
        inv, "/notes.md", "# B\nvalue: 1", "# B\nvalue: 2", **args
    )
    assert result.version == 2
    await service.append(
        inv, "/notes.md", "end\n", expected_version=2, idempotency_key="append-1"
    )
    replay = await service.edit(
        inv, "/notes.md", "# B\nvalue: 1", "# B\nvalue: 2", **args
    )
    assert replay.idempotent_replay and replay.version == 2
    assert (await service.read(inv, "/notes.md")).version == 3
    with pytest.raises(IdempotencyConflict):
        await service.edit(inv, "/notes.md", "# B\nvalue: 1", "different", **args)
    with pytest.raises(IdempotencyConflict):
        await service.write(inv, "/notes.md", "different", **args)
    old = await service.read_history(inv, "/notes.md", 2)
    assert (
        old.change_comment == "Update B"
        and old.committed_by_principal_id == "principal"
    )


async def test_concurrent_edits_have_one_winner_and_identical_retries_replay(
    postgres_store,
):
    service, inv = MemoryService(postgres_store), invocation()
    await service.write(inv, "/notes.md", "original", idempotency_key="seed")
    outcomes = await asyncio.gather(
        *(
            service.edit(
                inv,
                "/notes.md",
                "original",
                new,
                expected_version=1,
                idempotency_key=key,
            )
            for new, key in (("left", "left-key"), ("right", "right-key"))
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(x, VersionConflict) for x in outcomes) == 1
    current = await service.read(inv, "/notes.md")
    outcomes = await asyncio.gather(
        *(
            service.edit(
                inv,
                "/notes.md",
                current.content,
                "final",
                expected_version=2,
                idempotency_key="same-key",
            )
            for _ in range(2)
        )
    )
    assert {x.version for x in outcomes} == {3}
    assert sum(x.idempotent_replay for x in outcomes) == 1


async def test_edit_service_quota_rolls_back(postgres_store):
    service, inv = (
        MemoryService(postgres_store, max_content_bytes=10, max_append_bytes=10),
        invocation(),
    )
    await service.write(inv, "/notes.md", "aaaa", idempotency_key="seed")
    with pytest.raises(QuotaExceeded):
        await service.edit(
            inv,
            "/notes.md",
            "a",
            "12345",
            replace_all=True,
            expected_version=1,
            idempotency_key="expand",
        )
    assert (await service.read(inv, "/notes.md")).content == "aaaa"


async def test_mcp_workflow_and_scope_isolation_on_real_store(postgres_store):
    service, inv = MemoryService(postgres_store), invocation()
    await service.write(
        inv, "/ops/notes.md", "# Config\nRetries: 3\n", idempotency_key="seed"
    )
    other = replace(inv, scope=Scope("tools", "other-agent"))
    await service.write(
        other, "/private.md", "SECRET-OTHER-AGENT", idempotency_key="seed"
    )

    async def resolver(ctx, action):
        inv.require(action)
        return inv

    async with Client(create_mcp_server(service, resolver)) as client:
        listed = (
            await client.call_tool("memory_glob", {"pattern": "**/*.md"})
        ).structured_content
        assert listed["paths"] == ["/ops/notes.md"]
        found = (
            await client.call_tool("memory_grep", {"pattern": "Retries:"})
        ).structured_content
        assert found["matches"][0]["line_number"] == 2
        assert "SECRET" not in str(found)
        single = (
            await client.call_tool(
                "memory_grep", {"pattern": "Retries:", "path": "/ops/notes.md"}
            )
        ).structured_content
        assert single["matches"][0]["path"] == "/ops/notes.md"
        read = (
            await client.call_tool(
                "memory_read", {"path": "/ops/notes.md", "start_line": 2}
            )
        ).structured_content
        assert read["content"] == "Retries: 3\n"
        edited = (
            await client.call_tool(
                "memory_edit",
                {
                    "path": "/ops/notes.md",
                    "old_text": "Retries: 3",
                    "new_text": "Retries: 5",
                    "expected_version": read["version"],
                    "idempotency_key": "edit",
                },
            )
        ).structured_content
        assert edited["version"] == 2
    assert (
        await service.read(inv, "/ops/notes.md")
    ).content == "# Config\nRetries: 5\n"
