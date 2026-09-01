from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

import pytest

from agent_filetree_memory.domain.models import (
    DeleteResult,
    DocumentSnapshot,
    HistoricalDocument,
    MemoryAction,
    MemoryEntry,
    MemoryHistoryPage,
    MemoryVersion,
    Scope,
    VerifiedInvocation,
    WriteResult,
)


NOW = datetime(2026, 8, 28, 16, 0, tzinfo=timezone.utc)
PRIVATE_PATH = "/private/canary.md"
PRIVATE_CONTENT = "# PRIVATE-CONTENT-CANARY\n\nOnly the verified agent can read this."


class StubMemoryService:
    def __init__(self) -> None:
        self.snapshot = DocumentSnapshot(
            path=PRIVATE_PATH,
            content=PRIVATE_CONTENT,
            version=7,
            created_at=NOW - timedelta(days=1),
            version_created_at=NOW,
            committed_by_principal_id="principal-secret",
            co_authored_by=("agent:claude",),
            change_comment="Seed current version",
        )
        self.calls: list[tuple[str, VerifiedInvocation, dict[str, object]]] = []
        self.write_error: Exception | None = None
        self.append_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def list(
        self, invocation: VerifiedInvocation, path: str = "/"
    ) -> list[MemoryEntry]:
        invocation.require(MemoryAction.LIST)
        self.calls.append(("list", invocation, {"path": path}))
        if path == "/":
            return [
                MemoryEntry(
                    name="private",
                    path="/private",
                    kind="directory",
                    version=2,
                    version_created_at=NOW,
                )
            ]
        if path == "/private" and self.snapshot is not None:
            return [
                MemoryEntry(
                    name=PurePosixPath(self.snapshot.path).name,
                    path=self.snapshot.path,
                    kind="document",
                    version=self.snapshot.version,
                    version_created_at=self.snapshot.version_created_at,
                )
            ]
        return []

    async def read(
        self, invocation: VerifiedInvocation, path: str
    ) -> DocumentSnapshot:
        invocation.require(MemoryAction.READ)
        self.calls.append(("read", invocation, {"path": path}))
        assert self.snapshot is not None
        return self.snapshot

    async def list_history(
        self,
        invocation: VerifiedInvocation,
        path: str,
        *,
        limit: int = 20,
        before_version: int | None = None,
    ) -> MemoryHistoryPage:
        invocation.require(MemoryAction.HISTORY_LIST)
        self.calls.append(
            (
                "list_history",
                invocation,
                {
                    "path": path,
                    "limit": limit,
                    "before_version": before_version,
                },
            )
        )
        return MemoryHistoryPage(
            path=path,
            current_version=7,
            versions=(
                MemoryVersion(
                    version=7,
                    version_created_at=NOW,
                    committed_by_principal_id="principal-secret",
                    co_authored_by=("agent:claude",),
                    change_comment="Seed current version",
                ),
                MemoryVersion(
                    version=6,
                    version_created_at=NOW - timedelta(hours=1),
                    committed_by_principal_id="principal-previous",
                    change_comment="Previous version",
                ),
            ),
        )

    async def read_history(
        self,
        invocation: VerifiedInvocation,
        path: str,
        version: int,
        *,
        compare_to_version: int | None = None,
    ) -> HistoricalDocument:
        invocation.require(MemoryAction.HISTORY_READ)
        self.calls.append(
            (
                "read_history",
                invocation,
                {
                    "path": path,
                    "version": version,
                    "compare_to_version": compare_to_version,
                },
            )
        )
        return HistoricalDocument(
            path=path,
            content="# previous",
            version=version,
            version_created_at=NOW - timedelta(hours=1),
            committed_by_principal_id="principal-previous",
            change_comment="Previous version",
            compared_to_version=compare_to_version,
            diff="--- old\n+++ new\n" if compare_to_version is not None else None,
        )

    async def write(
        self,
        invocation: VerifiedInvocation,
        path: str,
        content: str,
        *,
        expected_version: int | None,
        idempotency_key: str,
        co_authored_by: Sequence[str] = (),
        change_comment: str | None = None,
    ) -> WriteResult:
        invocation.require(MemoryAction.WRITE)
        self.calls.append(
            (
                "write",
                invocation,
                {
                    "path": path,
                    "content": content,
                    "expected_version": expected_version,
                    "idempotency_key": idempotency_key,
                    "co_authored_by": co_authored_by,
                    "change_comment": change_comment,
                },
            )
        )
        if self.write_error is not None:
            raise self.write_error
        created = expected_version is None
        version = 1 if created else expected_version + 1
        self.snapshot = DocumentSnapshot(
            path,
            content,
            version,
            NOW,
            NOW,
            committed_by_principal_id=invocation.principal_id,
            co_authored_by=tuple(co_authored_by),
            change_comment=change_comment,
        )
        return WriteResult(path, version, created)

    async def append(
        self,
        invocation: VerifiedInvocation,
        path: str,
        content: str,
        *,
        expected_version: int,
        idempotency_key: str,
        co_authored_by: Sequence[str] = (),
        change_comment: str | None = None,
    ) -> WriteResult:
        invocation.require(MemoryAction.APPEND)
        self.calls.append(
            (
                "append",
                invocation,
                {
                    "path": path,
                    "content": content,
                    "expected_version": expected_version,
                    "idempotency_key": idempotency_key,
                    "co_authored_by": co_authored_by,
                    "change_comment": change_comment,
                },
            )
        )
        if self.append_error is not None:
            raise self.append_error
        assert self.snapshot is not None
        self.snapshot = DocumentSnapshot(
            path,
            self.snapshot.content + content,
            expected_version + 1,
            self.snapshot.created_at,
            NOW,
            committed_by_principal_id=invocation.principal_id,
            co_authored_by=tuple(co_authored_by),
            change_comment=change_comment,
        )
        return WriteResult(path, expected_version + 1, False)

    async def delete(
        self,
        invocation: VerifiedInvocation,
        path: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> DeleteResult:
        invocation.require(MemoryAction.DELETE)
        self.calls.append(
            (
                "delete",
                invocation,
                {
                    "path": path,
                    "expected_version": expected_version,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        if self.delete_error is not None:
            raise self.delete_error
        self.snapshot = None
        return DeleteResult(
            path=path,
            deleted_version=expected_version,
            purge_after=NOW + timedelta(days=30),
        )


class RecordingResolver:
    def __init__(self, invocation: VerifiedInvocation) -> None:
        self.invocation = invocation
        self.action_scoped = False
        self.calls: list[tuple[object, MemoryAction]] = []

    async def __call__(
        self, ctx: object, action: MemoryAction
    ) -> VerifiedInvocation:
        self.calls.append((ctx, action))
        if self.action_scoped:
            return replace(
                self.invocation,
                allowed_actions=frozenset({action}),
            )
        return self.invocation


@pytest.fixture
def verified_invocation() -> VerifiedInvocation:
    return VerifiedInvocation(
        scope=Scope(
            workspace_id="workspace-secret",
            agent_profile_id="agent-secret",
        ),
        principal_id="principal-secret",
        invocation_id="invocation-1",
        capability_id="capability-1",
        issuer="test-issuer",
        audience="test-audience",
        allowed_actions=frozenset(
            {
                MemoryAction.LIST,
                MemoryAction.READ,
                MemoryAction.HISTORY_LIST,
                MemoryAction.HISTORY_READ,
                MemoryAction.WRITE,
                MemoryAction.APPEND,
                MemoryAction.DELETE,
            }
        ),
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=3650),
    )


@pytest.fixture
def service() -> StubMemoryService:
    return StubMemoryService()


@pytest.fixture
def resolver(verified_invocation: VerifiedInvocation) -> RecordingResolver:
    return RecordingResolver(verified_invocation)
