"""Stable JSON payloads shared by the headless and MCP App adapters."""

from __future__ import annotations

from datetime import datetime
import sys
from typing import Literal, Sequence

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:
    from typing_extensions import TypedDict

from ..domain.models import (
    DeleteResult,
    DocumentSnapshot,
    HistoricalDocument,
    MemoryEntry,
    MemoryHistoryPage,
    MemoryVersion,
    WriteResult,
)
from ..domain.paths import normalize_memory_path


class MemoryEntryPayload(TypedDict):
    name: str
    path: str
    kind: str
    version: int
    version_created_at: str
    updated_at: str


class MemoryListPayload(TypedDict):
    path: str
    entries: list[MemoryEntryPayload]


class DocumentPayload(TypedDict):
    path: str
    content: str
    version: int
    created_at: str
    version_created_at: str
    updated_at: str
    committed_by: "AuthenticatedCommitterPayload | None"
    co_authored_by: list["DeclaredCoAuthorPayload"]
    change_comment: str | None


class AuthenticatedCommitterPayload(TypedDict):
    principal_id: str
    verification: Literal["authenticated"]


class DeclaredCoAuthorPayload(TypedDict):
    identifier: str
    verification: Literal["self_asserted"]


class MemoryVersionPayload(TypedDict):
    version: int
    version_created_at: str
    committed_by: AuthenticatedCommitterPayload | None
    co_authored_by: list[DeclaredCoAuthorPayload]
    change_comment: str | None


class MemoryHistoryPayload(TypedDict):
    path: str
    current_version: int
    versions: list[MemoryVersionPayload]
    next_before_version: int | None


class HistoricalDocumentPayload(TypedDict):
    path: str
    content: str
    version: int
    version_created_at: str
    committed_by: AuthenticatedCommitterPayload | None
    co_authored_by: list[DeclaredCoAuthorPayload]
    change_comment: str | None
    compared_to_version: int | None
    diff: str | None


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
        "version_created_at": _timestamp(entry.version_created_at),
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
        "version_created_at": _timestamp(snapshot.version_created_at),
        "updated_at": _timestamp(snapshot.updated_at),
        **_attribution_payload(
            snapshot.committed_by_principal_id,
            snapshot.co_authored_by,
        ),
        "change_comment": snapshot.change_comment,
    }


def _attribution_payload(
    committed_by_principal_id: str | None,
    co_authored_by: Sequence[str],
) -> dict[str, object]:
    return {
        "committed_by": (
            {
                "principal_id": committed_by_principal_id,
                "verification": "authenticated",
            }
            if committed_by_principal_id is not None
            else None
        ),
        "co_authored_by": [
            {"identifier": identifier, "verification": "self_asserted"}
            for identifier in co_authored_by
        ],
    }


def version_payload(version: MemoryVersion) -> MemoryVersionPayload:
    return {
        "version": version.version,
        "version_created_at": _timestamp(version.version_created_at),
        **_attribution_payload(
            version.committed_by_principal_id,
            version.co_authored_by,
        ),
        "change_comment": version.change_comment,
    }  # type: ignore[return-value]


def history_payload(page: MemoryHistoryPage) -> MemoryHistoryPayload:
    return {
        "path": page.path,
        "current_version": page.current_version,
        "versions": [version_payload(item) for item in page.versions],
        "next_before_version": page.next_before_version,
    }


def historical_document_payload(
    document: HistoricalDocument,
) -> HistoricalDocumentPayload:
    return {
        "path": document.path,
        "content": document.content,
        "version": document.version,
        "version_created_at": _timestamp(document.version_created_at),
        **_attribution_payload(
            document.committed_by_principal_id,
            document.co_authored_by,
        ),
        "change_comment": document.change_comment,
        "compared_to_version": document.compared_to_version,
        "diff": document.diff,
    }  # type: ignore[return-value]


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
