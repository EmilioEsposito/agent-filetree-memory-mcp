"""Persistence contract consumed by the application service."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence

from ..domain.models import (
    DeleteResult,
    DocumentSnapshot,
    MemoryEntry,
    Scope,
    WriteResult,
)


class MemoryStore(Protocol):
    async def list(
        self,
        scope: Scope,
        path: str,
        *,
        invocation_id: str | None = None,
        principal_id: str | None = None,
    ) -> Sequence[MemoryEntry]: ...

    async def read(
        self,
        scope: Scope,
        path: str,
        *,
        invocation_id: str | None = None,
        principal_id: str | None = None,
    ) -> DocumentSnapshot: ...

    async def write(
        self,
        scope: Scope,
        path: str,
        content: str,
        *,
        expected_version: int | None,
        idempotency_key: str,
        invocation_id: str,
        principal_id: str | None = None,
    ) -> WriteResult: ...

    async def append(
        self,
        scope: Scope,
        path: str,
        content: str,
        *,
        expected_version: int,
        idempotency_key: str,
        invocation_id: str,
        principal_id: str | None = None,
    ) -> WriteResult: ...

    async def delete(
        self,
        scope: Scope,
        path: str,
        *,
        expected_version: int,
        idempotency_key: str,
        invocation_id: str,
        principal_id: str | None = None,
    ) -> DeleteResult: ...

    async def export_markdown_tree(
        self,
        scope: Scope,
        path: str = "/",
        *,
        invocation_id: str | None = None,
        principal_id: str | None = None,
    ) -> Sequence[DocumentSnapshot]: ...

    async def purge_due(self, *, now: datetime, limit: int = 100) -> int: ...
