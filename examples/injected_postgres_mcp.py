"""Embed the package with either host-managed sessions or a static URL.

The database schema must be migrated before the MCP starts. The host remains
responsible for resolving each request into a verified invocation; no scope or
capability token is accepted by the memory tools themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.mcp import create_mcp_server
from agent_filetree_memory.ports import InvocationResolver
from agent_filetree_memory.postgres import (
    EnvelopeCodec,
    PostgresMemoryStore,
    PostgresRuntime,
    PostgresStoreConfig,
    SessionFactory,
)


@dataclass(slots=True)
class EmbeddedMemoryMCP:
    """Server plus the runtime whose lifecycle the host owns."""

    server: FastMCP
    runtime: PostgresRuntime


def from_session_factory(
    session_factory: SessionFactory,
    encryptor: EnvelopeCodec,
    invocation_resolver: InvocationResolver,
    store_config: PostgresStoreConfig,
    *,
    schema: str = "agent_filetree_memory",
    auth: Any = None,
    include_app: bool = False,
) -> EmbeddedMemoryMCP:
    """Borrow sessions from a platform-specific credential/database helper."""
    runtime = PostgresRuntime.from_session_factory(
        session_factory,
        schema=schema,
    )
    service = MemoryService(
        PostgresMemoryStore(runtime, encryptor, config=store_config)
    )
    server = create_mcp_server(
        service,
        invocation_resolver,
        auth=auth,
        include_app=include_app,
    )
    return EmbeddedMemoryMCP(server=server, runtime=runtime)


def from_database_url(
    database_url: str,
    encryptor: EnvelopeCodec,
    invocation_resolver: InvocationResolver,
    store_config: PostgresStoreConfig,
    *,
    schema: str = "agent_filetree_memory",
    auth: Any = None,
    include_app: bool = False,
) -> EmbeddedMemoryMCP:
    """Create an owned asyncpg engine from an explicit PostgreSQL URL."""
    runtime = PostgresRuntime.from_url(database_url, schema=schema)
    service = MemoryService(
        PostgresMemoryStore(runtime, encryptor, config=store_config)
    )
    server = create_mcp_server(
        service,
        invocation_resolver,
        auth=auth,
        include_app=include_app,
    )
    return EmbeddedMemoryMCP(server=server, runtime=runtime)
