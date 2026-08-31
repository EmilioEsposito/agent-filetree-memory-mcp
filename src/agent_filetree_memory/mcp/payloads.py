"""Stable JSON payloads shared by the headless and MCP App adapters."""

from __future__ import annotations

from datetime import datetime
import sys
from typing import Sequence

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

from ..domain.models import (
    DeleteResult,
    DocumentSnapshot,
    MemoryEntry,
    WriteResult,
)
from ..domain.paths import normalize_memory_path


class MemoryEntryPayload(TypedDict):
    name: str
    path: str
    kind: str
    version: int
    updated_at: str


class MemoryListPayload(TypedDict):
    path: str
    entries: list[MemoryEntryPayload]


class DocumentPayload(TypedDict):
    path: str
    content: str
    version: int
    created_at: str
    updated_at: str


class WritePayload(TypedDict):
    path: str
    version: int
    created: bool
    idempotent_replay: bool


class DeletePayload(TypedDict):
    path: str
    deleted_version: int
    purge_after: str
    idempotent_replay: bool


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def entry_payload(entry: MemoryEntry) -> MemoryEntryPayload:
    return {
        "name": entry.name,
        "path": entry.path,
        "kind": entry.kind,
        "version": entry.version,
        "updated_at": _timestamp(entry.updated_at),
    }


def list_payload(
    path: str, entries: Sequence[MemoryEntry]
) -> MemoryListPayload:
    return {
        "path": normalize_memory_path(path),
        "entries": [entry_payload(entry) for entry in entries],
    }


def document_payload(snapshot: DocumentSnapshot) -> DocumentPayload:
    return {
        "path": snapshot.path,
        "content": snapshot.content,
        "version": snapshot.version,
        "created_at": _timestamp(snapshot.created_at),
        "updated_at": _timestamp(snapshot.updated_at),
    }


def write_payload(result: WriteResult) -> WritePayload:
    return {
        "path": result.path,
        "version": result.version,
        "created": result.created,
        "idempotent_replay": result.idempotent_replay,
    }


def delete_payload(result: DeleteResult) -> DeletePayload:
    return {
        "path": result.path,
        "deleted_version": result.deleted_version,
        "purge_after": _timestamp(result.purge_after),
        "idempotent_replay": result.idempotent_replay,
    }
