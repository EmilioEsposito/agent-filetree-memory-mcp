from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

import pytest

from agent_filetree_memory.domain.models import (
    DeleteResult,
    DocumentSnapshot,
    MemoryAction,
    MemoryEntry,
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
            updated_at=NOW,
        )
        self.calls: list[tuple[str, VerifiedInvocation, dict[str, object]]] = []
        self.write_error: Exception | None = None
        self.append_error: Exception | None = None
        self.delete_error: Exception | None = None

    async def list(
        self, invocation: VerifiedInvocation, path: str = "/"
    ) -> list[MemoryEntry]:
        self.calls.append(("list", invocation, {"path": path}))
        if path == "/":
            return [
                MemoryEntry(
                    name="private",
                    path="/private",
                    kind="directory",
                    version=2,
                    updated_at=NOW,
                )
            ]
        if path == "/private" and self.snapshot is not None:
            return [
                MemoryEntry(
                    name=PurePosixPath(self.snapshot.path).name,
                    path=self.snapshot.path,
                    kind="document",
                    version=self.snapshot.version,
                    updated_at=self.snapshot.updated_at,
                )
            ]
        return []

    async def read(
        self, invocation: VerifiedInvocation, path: str
    ) -> DocumentSnapshot:
        self.calls.append(("read", invocation, {"path": path}))
        assert self.snapshot is not None
        return self.snapshot

    async def write(
        self,
        invocation: VerifiedInvocation,
        path: str,
        content: str,
        *,
        expected_version: int | None,
        idempotency_key: str,
    ) -> WriteResult:
        self.calls.append(
            (
                "write",
                invocation,
                {
                    "path": path,
                    "content": content,
                    "expected_version": expected_version,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        if self.write_error is not None:
            raise self.write_error
        created = expected_version is None
        version = 1 if created else expected_version + 1
        self.snapshot = DocumentSnapshot(path, content, version, NOW, NOW)
        return WriteResult(path, version, created)

    async def append(
        self,
        invocation: VerifiedInvocation,
        path: str,
        content: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> WriteResult:
        self.calls.append(
            (
                "append",
                invocation,
                {
                    "path": path,
                    "content": content,
                    "expected_version": expected_version,
                    "idempotency_key": idempotency_key,
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
        self.calls: list[tuple[object, MemoryAction]] = []

    async def __call__(
        self, ctx: object, action: MemoryAction
    ) -> VerifiedInvocation:
        self.calls.append((ctx, action))
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
