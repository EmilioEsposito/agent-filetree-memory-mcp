from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import (
    DeleteResult,
    DocumentSnapshot,
    HistoricalDocument,
    MemoryAction,
    MemoryHistoryPage,
    MemoryVersion,
    Scope,
    VerifiedInvocation,
    WriteResult,
)


NOW = datetime.now(timezone.utc)
SCOPE = Scope("workspace-1", "agent-1")
PRINCIPAL_ID = "principal-1"


def invocation(
    *actions: MemoryAction, expired: bool = False
) -> VerifiedInvocation:
    issued_at = NOW - timedelta(minutes=2)
    expires_at = NOW - timedelta(minutes=1) if expired else NOW + timedelta(hours=1)
    return VerifiedInvocation(
        scope=SCOPE,
        principal_id=PRINCIPAL_ID,
        invocation_id="invocation-1",
        capability_id="capability-1",
        issuer="test-issuer",
        audience="memory-service",
        allowed_actions=frozenset(actions),
        issued_at=issued_at,
        expires_at=expires_at,
    )


class RecordingStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def list(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append(("list", args, kwargs))
        return []

    async def read(self, *args: Any, **kwargs: Any) -> DocumentSnapshot:
        self.calls.append(("read", args, kwargs))
        return DocumentSnapshot("/notes.md", "body", 1, NOW, NOW)

    async def list_history(
        self, *args: Any, **kwargs: Any
    ) -> MemoryHistoryPage:
        self.calls.append(("list_history", args, kwargs))
        return MemoryHistoryPage(
            "/notes.md",
            1,
            (MemoryVersion(1, NOW),),
        )

    async def read_history(
        self, *args: Any, **kwargs: Any
    ) -> HistoricalDocument:
        self.calls.append(("read_history", args, kwargs))
        return HistoricalDocument("/notes.md", "body", 1, NOW)

    async def write(self, *args: Any, **kwargs: Any) -> WriteResult:
        self.calls.append(("write", args, kwargs))
        return WriteResult("/notes.md", 1, True)

    async def append(self, *args: Any, **kwargs: Any) -> WriteResult:
        self.calls.append(("append", args, kwargs))
        return WriteResult("/notes.md", 2, False)

    async def delete(self, *args: Any, **kwargs: Any) -> DeleteResult:
        self.calls.append(("delete", args, kwargs))
        return DeleteResult("/notes.md", 2, NOW + timedelta(days=1))

    async def export_markdown_tree(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append(("export", args, kwargs))
        return []


@pytest.mark.parametrize(
    ("action", "call"),
    [
        (MemoryAction.LIST, lambda service, inv: service.list(inv, "/")),
        (MemoryAction.READ, lambda service, inv: service.read(inv, "/a.md")),
        (
            MemoryAction.HISTORY_LIST,
            lambda service, inv: service.list_history(inv, "/a.md"),
        ),
        (
            MemoryAction.HISTORY_READ,
            lambda service, inv: service.read_history(inv, "/a.md", 1),
        ),
        (
            MemoryAction.WRITE,
            lambda service, inv: service.write(
                inv, "/a.md", "body", idempotency_key="write-1"
            ),
        ),
        (
            MemoryAction.APPEND,
            lambda service, inv: service.append(
                inv,
                "/a.md",
                "more",
                expected_version=1,
                idempotency_key="append-1",
            ),
        ),
        (
            MemoryAction.DELETE,
            lambda service, inv: service.delete(
                inv,
                "/a.md",
                expected_version=1,
                idempotency_key="delete-1",
            ),
        ),
        (MemoryAction.EXPORT, lambda service, inv: service.export(inv, "/")),
        (
            MemoryAction.IMPORT,
            lambda service, inv: service.import_markdown_tree(
                inv,
                {"/a.md": "body"},
                idempotency_namespace="import-1",
            ),
        ),
    ],
)
async def test_every_operation_authorizes_before_store_access(
    action: MemoryAction, call: Any
) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(AuthorizationDenied, match="not authorized"):
        await call(service, invocation(action, expired=True))

    assert store.calls == []


async def test_authorization_happens_before_request_validation() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(AuthorizationDenied):
        await service.write(
            invocation(MemoryAction.READ),
            "/../invalid",
            "secret",
            idempotency_key="bad key",
        )

    assert store.calls == []


async def test_rejects_non_invocation_objects_before_store_access() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(AuthorizationDenied):
        await service.list(object())  # type: ignore[arg-type]

    assert store.calls == []


async def test_invocation_copies_mutable_action_collection() -> None:
    store = RecordingStore()
    service = MemoryService(store)
    source_actions = {MemoryAction.LIST}
    mutable = VerifiedInvocation(
        scope=SCOPE,
        principal_id=PRINCIPAL_ID,
        invocation_id="invocation-1",
        capability_id="capability-1",
        issuer="test-issuer",
        audience="memory-service",
        allowed_actions=source_actions,  # type: ignore[arg-type]
        issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=1),
    )

    source_actions.add(MemoryAction.WRITE)
    await service.list(mutable)
    with pytest.raises(AuthorizationDenied):
        await service.write(
            mutable, "/notes.md", "body", idempotency_key="write-1"
        )

    assert mutable.allowed_actions == frozenset({MemoryAction.LIST})
    assert [call[0] for call in store.calls] == ["list"]


async def test_normalizes_and_forwards_write_without_changing_result() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    result = await service.write(
        invocation(MemoryAction.WRITE),
        "notes/cafe\u0301.md",
        "hello",
        expected_version=None,
        idempotency_key="request-1",
    )

    assert result == WriteResult("/notes.md", 1, True)
    name, args, kwargs = store.calls[0]
    assert name == "write"
    assert args == (SCOPE, "/notes/caf\u00e9.md", "hello")
    assert kwargs == {
        "expected_version": None,
        "idempotency_key": "request-1",
        "invocation_id": "invocation-1",
        "principal_id": PRINCIPAL_ID,
        "co_authored_by": (),
        "change_comment": None,
    }


async def test_forwards_compact_version_provenance_and_change_comment() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    await service.write(
        invocation(MemoryAction.WRITE),
        "/notes.md",
        "hello",
        idempotency_key="request-1",
        co_authored_by=("agent:claude", "agent:codex"),
        change_comment="Clarify the decision",
    )

    kwargs = store.calls[0][2]
    assert kwargs["principal_id"] == PRINCIPAL_ID
    assert kwargs["co_authored_by"] == ("agent:claude", "agent:codex")
    assert kwargs["change_comment"] == "Clarify the decision"


@pytest.mark.parametrize(
    ("action", "call", "store_method"),
    [
        (MemoryAction.LIST, lambda service, inv: service.list(inv), "list"),
        (
            MemoryAction.READ,
            lambda service, inv: service.read(inv, "/notes.md"),
            "read",
        ),
        (
            MemoryAction.HISTORY_LIST,
            lambda service, inv: service.list_history(
                inv,
                "/notes.md",
                limit=5,
                before_version=3,
            ),
            "list_history",
        ),
        (
            MemoryAction.HISTORY_READ,
            lambda service, inv: service.read_history(
                inv,
                "/notes.md",
                2,
                compare_to_version=1,
            ),
            "read_history",
        ),
        (MemoryAction.EXPORT, lambda service, inv: service.export(inv), "export"),
    ],
)
async def test_read_operations_forward_invocation_id(
    action: MemoryAction, call: Any, store_method: str
) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    await call(service, invocation(action))

    name, _, kwargs = store.calls[0]
    assert name == store_method
    assert kwargs["invocation_id"] == "invocation-1"
    assert kwargs["principal_id"] == PRINCIPAL_ID


@pytest.mark.parametrize(
    "co_authored_by",
    [
        "agent:claude",
        ("bad value",),
        ("agent:claude", "agent:claude"),
        tuple(f"agent:{index}" for index in range(9)),
    ],
)
async def test_rejects_invalid_declared_co_authors(
    co_authored_by: Any,
) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(ValueError, match="co_authored_by"):
        await service.write(
            invocation(MemoryAction.WRITE),
            "/notes.md",
            "body",
            idempotency_key="request-1",
            co_authored_by=co_authored_by,
        )

    assert store.calls == []


@pytest.mark.parametrize(
    "change_comment",
    ["", "   ", "bad\x00comment", "é" * 1025, 7],
)
async def test_rejects_invalid_change_comments(change_comment: Any) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(ValueError, match="change_comment"):
        await service.write(
            invocation(MemoryAction.WRITE),
            "/notes.md",
            "body",
            idempotency_key="request-1",
            change_comment=change_comment,
        )

    assert store.calls == []


@pytest.mark.parametrize("value", [0, -1, True, "2"])
async def test_history_versions_require_positive_integers(value: Any) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(ValueError, match="positive integer"):
        await service.read_history(
            invocation(MemoryAction.HISTORY_READ),
            "/notes.md",
            value,
        )

    assert store.calls == []


async def test_portable_import_validates_then_writes_in_stable_retry_order() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    results = await service.import_markdown_tree(
        invocation(MemoryAction.IMPORT),
        {
            "z.md": "# Z",
            "/notes/cafe\u0301.md": "# Cafe",
        },
        idempotency_namespace="portable-tree-1",
    )

    assert len(results) == 2
    assert [call[1][1] for call in store.calls] == [
        "/notes/caf\u00e9.md",
        "/z.md",
    ]
    keys = [call[2]["idempotency_key"] for call in store.calls]
    assert len(set(keys)) == 2
    assert all(len(key) == 64 and key.isascii() for key in keys)
    assert all(call[2]["expected_version"] is None for call in store.calls)


async def test_portable_import_rejects_the_whole_tree_before_first_write() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(ValueError, match="duplicate normalized paths"):
        await service.import_markdown_tree(
            invocation(MemoryAction.IMPORT),
            {"a.md": "first", "/a.md": "duplicate"},
            idempotency_namespace="portable-tree-2",
        )

    assert store.calls == []


@pytest.mark.parametrize("value", ["", " bad", "has space", "x" * 256, "é"])
async def test_rejects_invalid_idempotency_keys_without_store_access(
    value: str,
) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(ValueError, match="opaque identifier"):
        await service.write(
            invocation(MemoryAction.WRITE),
            "/notes.md",
            "body",
            idempotency_key=value,
        )

    assert store.calls == []


async def test_accepts_maximum_length_opaque_idempotency_key() -> None:
    store = RecordingStore()
    service = MemoryService(store)

    await service.write(
        invocation(MemoryAction.WRITE),
        "/notes.md",
        "body",
        idempotency_key="x" * 255,
    )

    assert store.calls[0][2]["idempotency_key"] == "x" * 255


@pytest.mark.parametrize("version", [0, -1, True, 1.5, "1", None])
async def test_append_requires_a_positive_integer_version(version: Any) -> None:
    store = RecordingStore()
    service = MemoryService(store)

    with pytest.raises(ValueError, match="positive integer"):
        await service.append(
            invocation(MemoryAction.APPEND),
            "/notes.md",
            "body",
            expected_version=version,
            idempotency_key="append-1",
        )

    assert store.calls == []


async def test_content_limits_use_utf8_bytes() -> None:
    store = RecordingStore()
    service = MemoryService(store, max_content_bytes=4, max_append_bytes=4)

    await service.write(
        invocation(MemoryAction.WRITE),
        "/notes.md",
        "éé",
        idempotency_key="request-1",
    )
    with pytest.raises(ValueError, match="configured limits"):
        await service.write(
            invocation(MemoryAction.WRITE),
            "/other.md",
            "ééé",
            idempotency_key="request-2",
        )

    assert [call[0] for call in store.calls] == ["write"]


async def test_append_rejects_empty_text_and_text_with_nul() -> None:
    store = RecordingStore()
    service = MemoryService(store)
    inv = invocation(MemoryAction.APPEND)

    for content in ("", "not\x00text", "\ud800"):
        with pytest.raises(ValueError, match="valid text"):
            await service.append(
                inv,
                "/notes.md",
                content,
                expected_version=1,
                idempotency_key="append-1",
            )

    assert store.calls == []


async def test_application_logs_neither_content_nor_request_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = RecordingStore()
    service = MemoryService(store)
    content = "highly-sensitive-memory-marker"
    path = "/private-path-marker.md"
    key = "private-idempotency-marker"
    co_author = "agent:private-coauthor-marker"
    change_comment = "private-change-comment-marker"

    with caplog.at_level("INFO"):
        await service.write(
            invocation(MemoryAction.WRITE),
            path,
            content,
            idempotency_key=key,
            co_authored_by=(co_author,),
            change_comment=change_comment,
        )

    log_output = "\n".join(record.getMessage() for record in caplog.records)
    assert content not in log_output
    assert path not in log_output
    assert key not in log_output
    assert co_author not in log_output
    assert change_comment not in log_output
    assert "memory operation completed" in log_output


def test_verified_invocation_and_scope_are_frozen() -> None:
    verified = invocation(MemoryAction.READ)
    with pytest.raises(FrozenInstanceError):
        verified.invocation_id = "replaced"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        verified.scope.agent_profile_id = "replaced"  # type: ignore[misc]
