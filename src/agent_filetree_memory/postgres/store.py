"""Encrypted virtual file-tree persistence for PostgreSQL."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.errors import (
    IdempotencyConflict,
    IntegrityFailure,
    InvalidMemoryPath,
    NotFoundOrDenied,
    QuotaExceeded,
    RateLimitExceeded,
    VersionConflict,
)
from ..domain.models import (
    DeleteResult,
    DocumentSnapshot,
    MemoryEntry,
    Scope,
    WriteResult,
    validate_opaque_id,
)
from ..domain.paths import normalize_memory_path
from ..ports.crypto import EncryptedPayload, EncryptionContext
from .runtime import PostgresRuntime
from .janitor import PostgresJanitor


class EnvelopeCodec(Protocol):
    """The narrow portion of ``EnvelopeEncryptor`` required by this adapter."""

    async def encrypt(
        self, plaintext: bytes, context: EncryptionContext
    ) -> EncryptedPayload: ...

    async def decrypt(
        self, payload: EncryptedPayload, context: EncryptionContext
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PostgresStoreConfig:
    """Bounded storage policy enforced transactionally per exact scope.

    ``idempotency_index_key`` is raw secret key material for the persistent
    HMAC blind index. Hosts must keep it stable across restarts and should
    derive it with domain separation or provision it separately from the
    envelope-encryption KEK.
    """

    idempotency_index_key: bytes = field(repr=False)
    retention_window: timedelta = timedelta(days=30)
    idempotency_ttl: timedelta = timedelta(days=1)
    audit_retention_window: timedelta = timedelta(days=90)
    max_document_bytes: int = 1_048_576
    max_scope_bytes: int = 50 * 1_048_576
    max_documents: int = 10_000
    max_physical_objects: int = 20_000
    max_versions_per_object: int = 32
    max_path_depth: int = 32
    rate_limit_operations: int = 600
    rate_window: timedelta = timedelta(minutes=1)
    service_namespace: str = "agent-filetree-memory"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.idempotency_index_key, bytes)
            or len(self.idempotency_index_key) < 32
        ):
            raise ValueError("idempotency_index_key must contain at least 32 bytes")
        if self.retention_window <= timedelta(0):
            raise ValueError("retention_window must be positive")
        if self.idempotency_ttl <= timedelta(0):
            raise ValueError("idempotency_ttl must be positive")
        if self.audit_retention_window <= timedelta(0):
            raise ValueError("audit_retention_window must be positive")
        if self.rate_window <= timedelta(0):
            raise ValueError("rate_window must be positive")
        for name in (
            "max_document_bytes",
            "max_scope_bytes",
            "max_documents",
            "max_physical_objects",
            "max_versions_per_object",
            "max_path_depth",
            "rate_limit_operations",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        if self.max_document_bytes > self.max_scope_bytes:
            raise ValueError("max_document_bytes cannot exceed max_scope_bytes")
        if self.max_path_depth > 64:
            raise ValueError("max_path_depth cannot exceed 64")
        validate_opaque_id(self.service_namespace, field="service_namespace")


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    name: str
    object_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class _IdempotencyReplay:
    fingerprint: str
    result: Mapping[str, Any]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _scope_values(scope: Scope) -> dict[str, str]:
    return {
        "workspace_id": scope.workspace_id,
        "agent_profile_id": scope.agent_profile_id,
    }


def _scope_predicate(table: Any, scope: Scope) -> Any:
    return and_(
        table.c.workspace_id == scope.workspace_id,
        table.c.agent_profile_id == scope.agent_profile_id,
    )


def _object_predicate(table: Any, scope: Scope, object_id: str) -> Any:
    return and_(
        _scope_predicate(table, scope),
        table.c.object_id == object_id,
    )


def _opaque_uuid() -> str:
    return str(uuid4())


def _validate_object_id(value: object) -> str:
    if not isinstance(value, str):
        raise IntegrityFailure("encrypted manifest is malformed")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise IntegrityFailure("encrypted manifest is malformed") from exc
    if str(parsed) != value:
        raise IntegrityFailure("encrypted manifest is malformed")
    return value


def _encode_manifest(entries: Sequence[_ManifestEntry]) -> bytes:
    body = {
        "format": 1,
        "entries": [
            {"kind": item.kind, "name": item.name, "object_id": item.object_id}
            for item in sorted(entries, key=lambda item: item.name)
        ],
    }
    return json.dumps(
        body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _decode_manifest(raw: bytes) -> tuple[_ManifestEntry, ...]:
    try:
        body = json.loads(raw.decode("utf-8"))
        if body.get("format") != 1 or not isinstance(body.get("entries"), list):
            raise ValueError
        decoded: list[_ManifestEntry] = []
        seen: set[str] = set()
        for value in body["entries"]:
            if not isinstance(value, dict) or set(value) != {
                "kind",
                "name",
                "object_id",
            }:
                raise ValueError
            name = value["name"]
            kind = value["kind"]
            if not isinstance(name, str) or not isinstance(kind, str):
                raise ValueError
            normalized = normalize_memory_path("/" + name, allow_root=False)
            if normalized != "/" + name or "/" in name:
                raise ValueError
            if kind not in {"directory", "document"} or name in seen:
                raise ValueError
            seen.add(name)
            decoded.append(
                _ManifestEntry(
                    name=name,
                    object_id=_validate_object_id(value["object_id"]),
                    kind=kind,
                )
            )
        return tuple(decoded)
    except IntegrityFailure:
        raise
    except Exception as exc:
        raise IntegrityFailure("encrypted manifest is malformed") from exc


def _fingerprint(operation: str, **values: object) -> str:
    encoded = json.dumps(
        {"operation": operation, **values},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _advisory_key(*parts: str) -> int:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _safe_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, (NotFoundOrDenied, QuotaExceeded, RateLimitExceeded)):
        return "denied", type(exc).__name__.lower()
    if isinstance(exc, (VersionConflict, IdempotencyConflict)):
        return "conflict", type(exc).__name__.lower()
    if isinstance(exc, IntegrityFailure):
        return "failed", "integrity_failure"
    return "failed", "operation_failure"


class PostgresMemoryStore:
    """A capability-scope-preserving implementation of ``MemoryStore``.

    Authorization is deliberately outside this adapter. Every supplied
    ``Scope`` is nevertheless included in every tenant object query, so an
    opaque object ID is never sufficient to cross a scope boundary.
    """

    def __init__(
        self,
        runtime: PostgresRuntime,
        encryptor: EnvelopeCodec,
        *,
        config: PostgresStoreConfig,
    ) -> None:
        if not hasattr(encryptor, "encrypt") or not hasattr(encryptor, "decrypt"):
            raise TypeError("encryptor must implement encrypt and decrypt")
        self.runtime = runtime
        self.encryptor = encryptor
        if not isinstance(config, PostgresStoreConfig):
            raise TypeError("config must be PostgresStoreConfig")
        self.config = config
        self.tables = runtime.tables

    async def list(
        self,
        scope: Scope,
        path: str,
        *,
        invocation_id: str | None = None,
        principal_id: str | None = None,
    ) -> Sequence[MemoryEntry]:
        normalized = self._normalize_path(path)
        self._validate_optional_invocation(invocation_id)
        self._validate_optional_principal(principal_id)
        try:
            await self._consume_rate(scope)
            async with self.runtime.session() as session, session.begin():
                resolved = await self._resolve(session, scope, normalized)
                if resolved is None:
                    if normalized == "/":
                        result: Sequence[MemoryEntry] = ()
                        await self._audit(
                            session,
                            scope,
                            "memory:list",
                            "succeeded",
                            invocation_id=invocation_id,
                            principal_id=principal_id,
                        )
                        return result
                    raise NotFoundOrDenied("memory object is unavailable")
                if resolved["object_kind"] not in {"root", "directory"}:
                    raise NotFoundOrDenied("memory object is unavailable")
                manifest = await self._load_manifest(session, scope, resolved)
                object_ids = [entry.object_id for entry in manifest]
                rows_by_id: dict[str, Mapping[str, Any]] = {}
                if object_ids:
                    rows = (
                        await session.execute(
                            select(self.tables.objects).where(
                                _scope_predicate(self.tables.objects, scope),
                                self.tables.objects.c.object_id.in_(object_ids),
                                self.tables.objects.c.lifecycle == "active",
                            )
                        )
                    ).mappings()
                    rows_by_id = {row["object_id"]: row for row in rows}
                entries: list[MemoryEntry] = []
                for item in manifest:
                    row = rows_by_id.get(item.object_id)
                    if row is None or row["object_kind"] != item.kind:
                        raise IntegrityFailure("encrypted manifest target is invalid")
                    child_path = (
                        "/" + item.name
                        if normalized == "/"
                        else normalized + "/" + item.name
                    )
                    entries.append(
                        MemoryEntry(
                            name=item.name,
                            path=child_path,
                            kind=item.kind,
                            version=row["current_version"],
                            updated_at=row["updated_at"],
                        )
                    )
                await self._audit(
                    session,
                    scope,
                    "memory:list",
                    "succeeded",
                    invocation_id=invocation_id,
                    principal_id=principal_id,
                    object_id=resolved["object_id"],
                )
                return tuple(entries)
        except Exception as exc:
            await self._audit_failure(
                scope,
                "memory:list",
                exc,
                invocation_id=invocation_id,
                principal_id=principal_id,
            )
            raise

    async def read(
        self,
        scope: Scope,
        path: str,
        *,
        invocation_id: str | None = None,
        principal_id: str | None = None,
    ) -> DocumentSnapshot:
        normalized = self._normalize_path(path, allow_root=False)
        self._validate_optional_invocation(invocation_id)
        self._validate_optional_principal(principal_id)
        try:
            await self._consume_rate(scope)
            async with self.runtime.session() as session, session.begin():
                resolved = await self._resolve(session, scope, normalized)
                if resolved is None or resolved["object_kind"] != "document":
                    raise NotFoundOrDenied("memory object is unavailable")
                raw, version_created = await self._load_current_payload(
                    session, scope, resolved
                )
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise IntegrityFailure("encrypted document is malformed") from exc
                await self._audit(
                    session,
                    scope,
                    "memory:read",
                    "succeeded",
                    invocation_id=invocation_id,
                    principal_id=principal_id,
                    object_id=resolved["object_id"],
                )
                return DocumentSnapshot(
                    path=normalized,
                    content=content,
                    version=resolved["current_version"],
                    created_at=resolved["created_at"],
                    updated_at=version_created,
                )
        except Exception as exc:
            await self._audit_failure(
                scope,
                "memory:read",
                exc,
                invocation_id=invocation_id,
                principal_id=principal_id,
            )
            raise

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
    ) -> WriteResult:
        normalized = self._normalize_path(path, allow_root=False)
        raw = self._validate_content(content)
        self._validate_write_inputs(expected_version, idempotency_key, invocation_id)
        self._validate_optional_principal(principal_id)
        fingerprint = _fingerprint(
            "write",
            path=normalized,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            expected_version=expected_version,
        )
        try:
            await self._consume_rate(scope)
            async with self.runtime.session() as session, session.begin():
                await self._lock_scope(session, scope)
                replay = await self._find_idempotency(
                    session, scope, idempotency_key, fingerprint
                )
                if replay is not None:
                    await self._audit(
                        session,
                        scope,
                        "memory:write",
                        "succeeded",
                        invocation_id=invocation_id,
                        principal_id=principal_id,
                    )
                    return WriteResult(
                        path=normalized,
                        version=int(replay.result["version"]),
                        created=bool(replay.result["created"]),
                        idempotent_replay=True,
                    )
                result, object_id = await self._write_new_version(
                    session,
                    scope,
                    normalized,
                    raw,
                    expected_version=expected_version,
                )
                await self._store_idempotency(
                    session,
                    scope,
                    idempotency_key,
                    fingerprint,
                    {"created": result.created, "version": result.version},
                )
                await self._audit(
                    session,
                    scope,
                    "memory:write",
                    "succeeded",
                    invocation_id=invocation_id,
                    principal_id=principal_id,
                    object_id=object_id,
                )
                return result
        except Exception as exc:
            await self._audit_failure(
                scope,
                "memory:write",
                exc,
                invocation_id=invocation_id,
                principal_id=principal_id,
            )
            raise

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
    ) -> WriteResult:
        normalized = self._normalize_path(path, allow_root=False)
        appended = self._validate_content(content, enforce_document_limit=False)
        self._validate_required_version(expected_version)
        validate_opaque_id(idempotency_key, field="idempotency_key")
        validate_opaque_id(invocation_id, field="invocation_id")
        self._validate_optional_principal(principal_id)
        fingerprint = _fingerprint(
            "append",
            path=normalized,
            content_sha256=hashlib.sha256(appended).hexdigest(),
            expected_version=expected_version,
        )
        try:
            await self._consume_rate(scope)
            async with self.runtime.session() as session, session.begin():
                await self._lock_scope(session, scope)
                replay = await self._find_idempotency(
                    session, scope, idempotency_key, fingerprint
                )
                if replay is not None:
                    await self._audit(
                        session,
                        scope,
                        "memory:append",
                        "succeeded",
                        invocation_id=invocation_id,
                        principal_id=principal_id,
                    )
                    return WriteResult(
                        path=normalized,
                        version=int(replay.result["version"]),
                        created=False,
                        idempotent_replay=True,
                    )
                resolved = await self._resolve(session, scope, normalized, for_update=True)
                if resolved is None or resolved["object_kind"] != "document":
                    raise NotFoundOrDenied("memory object is unavailable")
                if resolved["current_version"] != expected_version:
                    raise VersionConflict("document version does not match")
                current, _ = await self._load_current_payload(session, scope, resolved)
                combined = current + appended
                if len(combined) > self.config.max_document_bytes:
                    raise QuotaExceeded("document quota would be exceeded")
                result = await self._replace_document(
                    session, scope, normalized, resolved, combined
                )
                await self._store_idempotency(
                    session,
                    scope,
                    idempotency_key,
                    fingerprint,
                    {"created": False, "version": result.version},
                )
                await self._audit(
                    session,
                    scope,
                    "memory:append",
                    "succeeded",
                    invocation_id=invocation_id,
                    principal_id=principal_id,
                    object_id=resolved["object_id"],
                )
                return result
        except Exception as exc:
            await self._audit_failure(
                scope,
                "memory:append",
                exc,
                invocation_id=invocation_id,
                principal_id=principal_id,
            )
            raise

    async def delete(
        self,
        scope: Scope,
        path: str,
        *,
        expected_version: int,
        idempotency_key: str,
        invocation_id: str,
        principal_id: str | None = None,
    ) -> DeleteResult:
        normalized = self._normalize_path(path, allow_root=False)
        self._validate_required_version(expected_version)
        validate_opaque_id(idempotency_key, field="idempotency_key")
        validate_opaque_id(invocation_id, field="invocation_id")
        self._validate_optional_principal(principal_id)
        fingerprint = _fingerprint(
            "delete", path=normalized, expected_version=expected_version
        )
        try:
            await self._consume_rate(scope)
            async with self.runtime.session() as session, session.begin():
                await self._lock_scope(session, scope)
                replay = await self._find_idempotency(
                    session, scope, idempotency_key, fingerprint
                )
                if replay is not None:
                    await self._audit(
                        session,
                        scope,
                        "memory:delete",
                        "succeeded",
                        invocation_id=invocation_id,
                        principal_id=principal_id,
                    )
                    return DeleteResult(
                        path=normalized,
                        deleted_version=int(replay.result["deleted_version"]),
                        purge_after=datetime.fromisoformat(
                            str(replay.result["purge_after"])
                        ),
                        idempotent_replay=True,
                    )
                parent, manifest, leaf = await self._resolve_parent(
                    session, scope, normalized, create=False
                )
                entry = next((item for item in manifest if item.name == leaf), None)
                if entry is None:
                    raise NotFoundOrDenied("memory object is unavailable")
                resolved = await self._load_object(
                    session, scope, entry.object_id, for_update=True
                )
                if (
                    resolved is None
                    or resolved["lifecycle"] != "active"
                    or resolved["object_kind"] != "document"
                    or entry.kind != "document"
                ):
                    raise NotFoundOrDenied("memory object is unavailable")
                if resolved["current_version"] != expected_version:
                    raise VersionConflict("document version does not match")
                purge_after = _now() + self.config.retention_window
                changed = await session.execute(
                    update(self.tables.objects)
                    .where(
                        _object_predicate(
                            self.tables.objects, scope, resolved["object_id"]
                        ),
                        self.tables.objects.c.lifecycle == "active",
                        self.tables.objects.c.current_version == expected_version,
                    )
                    .values(
                        lifecycle="deleted",
                        purge_after=purge_after,
                        updated_at=_now(),
                    )
                )
                if changed.rowcount != 1:
                    raise VersionConflict("document version does not match")
                await self._replace_manifest(
                    session,
                    scope,
                    parent,
                    tuple(item for item in manifest if item.name != leaf),
                )
                await self._change_quota(
                    session,
                    scope,
                    bytes_delta=-int(resolved["logical_bytes"]),
                    documents_delta=-1,
                )
                result = DeleteResult(
                    path=normalized,
                    deleted_version=expected_version,
                    purge_after=purge_after,
                )
                await self._store_idempotency(
                    session,
                    scope,
                    idempotency_key,
                    fingerprint,
                    {
                        "deleted_version": expected_version,
                        "purge_after": purge_after.isoformat(),
                    },
                )
                await self._audit(
                    session,
                    scope,
                    "memory:delete",
                    "succeeded",
                    invocation_id=invocation_id,
                    principal_id=principal_id,
                    object_id=resolved["object_id"],
                )
                return result
        except Exception as exc:
            await self._audit_failure(
                scope,
                "memory:delete",
                exc,
                invocation_id=invocation_id,
                principal_id=principal_id,
            )
            raise

    async def export_markdown_tree(
        self,
        scope: Scope,
        path: str = "/",
        *,
        invocation_id: str | None = None,
        principal_id: str | None = None,
    ) -> Sequence[DocumentSnapshot]:
        normalized = self._normalize_path(path)
        self._validate_optional_invocation(invocation_id)
        self._validate_optional_principal(principal_id)
        try:
            await self._consume_rate(scope)
            async with self.runtime.session() as session, session.begin():
                resolved = await self._resolve(session, scope, normalized)
                if resolved is None:
                    if normalized == "/":
                        await self._audit(
                            session,
                            scope,
                            "memory:export",
                            "succeeded",
                            invocation_id=invocation_id,
                            principal_id=principal_id,
                        )
                        return ()
                    raise NotFoundOrDenied("memory object is unavailable")
                snapshots: list[DocumentSnapshot] = []
                await self._export_object(
                    session, scope, normalized, resolved, snapshots
                )
                snapshots.sort(key=lambda item: item.path)
                await self._audit(
                    session,
                    scope,
                    "memory:export",
                    "succeeded",
                    invocation_id=invocation_id,
                    principal_id=principal_id,
                    object_id=resolved["object_id"],
                )
                return tuple(snapshots)
        except Exception as exc:
            await self._audit_failure(
                scope,
                "memory:export",
                exc,
                invocation_id=invocation_id,
                principal_id=principal_id,
            )
            raise

    async def purge_due(self, *, now: datetime, limit: int = 100) -> int:
        report = await PostgresJanitor(
            self.runtime,
            audit_retention_window=self.config.audit_retention_window,
        ).purge_due(now=now, limit=limit)
        return report.deleted_objects

    def _validate_content(
        self, content: str, *, enforce_document_limit: bool = True
    ) -> bytes:
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        raw = content.encode("utf-8")
        if enforce_document_limit and len(raw) > self.config.max_document_bytes:
            raise QuotaExceeded("document quota would be exceeded")
        return raw

    def _normalize_path(self, path: str, *, allow_root: bool = True) -> str:
        normalized = normalize_memory_path(path, allow_root=allow_root)
        depth = 0 if normalized == "/" else normalized.count("/")
        if depth > self.config.max_path_depth:
            raise InvalidMemoryPath("invalid memory path")
        return normalized

    def _validate_optional_invocation(self, invocation_id: str | None) -> None:
        if invocation_id is not None:
            validate_opaque_id(invocation_id, field="invocation_id")

    def _validate_optional_principal(self, principal_id: str | None) -> None:
        if principal_id is not None:
            validate_opaque_id(principal_id, field="principal_id")

    def _validate_required_version(self, version: int) -> None:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("expected_version must be a positive integer")

    def _validate_write_inputs(
        self,
        expected_version: int | None,
        idempotency_key: str,
        invocation_id: str,
    ) -> None:
        if expected_version is not None:
            self._validate_required_version(expected_version)
        validate_opaque_id(idempotency_key, field="idempotency_key")
        validate_opaque_id(invocation_id, field="invocation_id")

    async def _lock_scope(self, session: AsyncSession, scope: Scope) -> None:
        lock_key = _advisory_key("memory-scope", *_scope_values(scope).values())
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": lock_key}
        )

    async def _consume_rate(self, scope: Scope) -> None:
        async with self.runtime.session() as session, session.begin():
            count = await self._increment_rate(session, scope)
        # Raise only after the increment has committed. Rejected, guessed, and
        # tampered operations therefore consume the same budget as successes.
        if count > self.config.rate_limit_operations:
            raise RateLimitExceeded("memory operation rate exceeded")

    async def _increment_rate(self, session: AsyncSession, scope: Scope) -> int:
        now = _now()
        seconds = self.config.rate_window.total_seconds()
        bucket_epoch = int(now.timestamp() // seconds * seconds)
        bucket = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
        expires = bucket + self.config.rate_window * 2
        table = self.tables.rate_buckets
        statement = (
            pg_insert(table)
            .values(
                **_scope_values(scope),
                bucket_started_at=bucket,
                operation_count=1,
                expires_at=expires,
            )
            .on_conflict_do_update(
                index_elements=[
                    table.c.workspace_id,
                    table.c.agent_profile_id,
                    table.c.bucket_started_at,
                ],
                set_={"operation_count": table.c.operation_count + 1},
            )
            .returning(table.c.operation_count)
        )
        return int((await session.execute(statement)).scalar_one())

    def _context(
        self,
        scope: Scope,
        *,
        purpose: str,
        object_id: str,
        object_kind: str,
        version: int,
        format_version: int = 1,
    ) -> EncryptionContext:
        return EncryptionContext(
            purpose=purpose,
            workspace_id=scope.workspace_id,
            agent_profile_id=scope.agent_profile_id,
            object_id=object_id,
            object_kind=object_kind,
            version=version,
            service_namespace=self.config.service_namespace,
            format_version=format_version,
        )

    async def _encrypt(
        self,
        raw: bytes,
        scope: Scope,
        *,
        purpose: str,
        object_id: str,
        object_kind: str,
        version: int,
    ) -> EncryptedPayload:
        try:
            return await self.encryptor.encrypt(
                raw,
                self._context(
                    scope,
                    purpose=purpose,
                    object_id=object_id,
                    object_kind=object_kind,
                    version=version,
                ),
            )
        except IntegrityFailure:
            raise
        except Exception as exc:
            raise IntegrityFailure("encryption operation failed") from exc

    async def _decrypt_row(
        self,
        row: Mapping[str, Any],
        scope: Scope,
        *,
        purpose: str,
        object_id: str,
        object_kind: str,
        version: int,
    ) -> bytes:
        payload = EncryptedPayload(
            ciphertext=bytes(row["ciphertext"]),
            wrapped_dek=bytes(row["wrapped_dek"]),
            provider_id=row["provider_id"],
            key_id=row["key_id"],
            format_version=row["format_version"],
        )
        try:
            return await self.encryptor.decrypt(
                payload,
                self._context(
                    scope,
                    purpose=purpose,
                    object_id=object_id,
                    object_kind=object_kind,
                    version=version,
                    format_version=row["format_version"],
                ),
            )
        except IntegrityFailure:
            raise
        except Exception as exc:
            raise IntegrityFailure("encrypted storage authentication failed") from exc

    async def _insert_object(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        object_id: str,
        kind: str,
        logical_bytes: int = 0,
    ) -> None:
        await self._change_quota(
            session,
            scope,
            bytes_delta=0,
            documents_delta=0,
            physical_objects_delta=1,
        )
        now = _now()
        await session.execute(
            insert(self.tables.objects).values(
                **_scope_values(scope),
                object_id=object_id,
                object_kind=kind,
                current_version=1,
                lifecycle="active",
                logical_bytes=logical_bytes,
                created_at=now,
                updated_at=now,
            )
        )
    async def _insert_version(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        object_id: str,
        kind: str,
        version: int,
        raw: bytes,
    ) -> None:
        purpose = "memory-document" if kind == "document" else "memory-manifest"
        payload = await self._encrypt(
            raw,
            scope,
            purpose=purpose,
            object_id=object_id,
            object_kind=kind,
            version=version,
        )
        await session.execute(
            insert(self.tables.versions).values(
                **_scope_values(scope),
                object_id=object_id,
                version=version,
                ciphertext=payload.ciphertext,
                wrapped_dek=payload.wrapped_dek,
                provider_id=payload.provider_id,
                key_id=payload.key_id,
                format_version=payload.format_version,
                created_at=_now(),
            )
        )
        oldest_retained = version - self.config.max_versions_per_object
        if oldest_retained >= 1:
            await session.execute(
                delete(self.tables.versions).where(
                    _object_predicate(
                        self.tables.versions,
                        scope,
                        object_id,
                    ),
                    self.tables.versions.c.version == oldest_retained,
                )
            )

    async def _ensure_root(
        self, session: AsyncSession, scope: Scope
    ) -> Mapping[str, Any]:
        root = await self._load_root(session, scope, for_update=True)
        if root is not None:
            return root
        object_id = _opaque_uuid()
        await self._insert_object(
            session, scope, object_id=object_id, kind="root"
        )
        await self._insert_version(
            session,
            scope,
            object_id=object_id,
            kind="root",
            version=1,
            raw=_encode_manifest(()),
        )
        root = await self._load_object(session, scope, object_id, for_update=True)
        if root is None:  # defensive; the insert and lookup share one transaction.
            raise IntegrityFailure("root object could not be created")
        return root

    async def _load_root(
        self, session: AsyncSession, scope: Scope, *, for_update: bool = False
    ) -> Mapping[str, Any] | None:
        statement = select(self.tables.objects).where(
            _scope_predicate(self.tables.objects, scope),
            self.tables.objects.c.object_kind == "root",
            self.tables.objects.c.lifecycle == "active",
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).mappings().one_or_none()

    async def _load_object(
        self,
        session: AsyncSession,
        scope: Scope,
        object_id: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        statement = select(self.tables.objects).where(
            _object_predicate(self.tables.objects, scope, object_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return (await session.execute(statement)).mappings().one_or_none()

    async def _load_version(
        self,
        session: AsyncSession,
        scope: Scope,
        object_id: str,
        version: int,
    ) -> Mapping[str, Any]:
        row = (
            await session.execute(
                select(self.tables.versions).where(
                    _object_predicate(self.tables.versions, scope, object_id),
                    self.tables.versions.c.version == version,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            raise IntegrityFailure("encrypted object version is unavailable")
        return row

    async def _load_current_payload(
        self,
        session: AsyncSession,
        scope: Scope,
        object_row: Mapping[str, Any],
    ) -> tuple[bytes, datetime]:
        version = int(object_row["current_version"])
        row = await self._load_version(
            session, scope, object_row["object_id"], version
        )
        kind = object_row["object_kind"]
        purpose = "memory-document" if kind == "document" else "memory-manifest"
        raw = await self._decrypt_row(
            row,
            scope,
            purpose=purpose,
            object_id=object_row["object_id"],
            object_kind=kind,
            version=version,
        )
        return raw, row["created_at"]

    async def _load_manifest(
        self,
        session: AsyncSession,
        scope: Scope,
        object_row: Mapping[str, Any],
    ) -> tuple[_ManifestEntry, ...]:
        if object_row["object_kind"] not in {"root", "directory"}:
            raise IntegrityFailure("object is not a directory manifest")
        raw, _ = await self._load_current_payload(session, scope, object_row)
        return _decode_manifest(raw)

    async def _resolve(
        self,
        session: AsyncSession,
        scope: Scope,
        path: str,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        current = await self._load_root(session, scope, for_update=for_update)
        if current is None:
            return None
        if path == "/":
            return current
        for segment in path[1:].split("/"):
            if current["object_kind"] not in {"root", "directory"}:
                return None
            manifest = await self._load_manifest(session, scope, current)
            entry = next((item for item in manifest if item.name == segment), None)
            if entry is None:
                return None
            current = await self._load_object(
                session, scope, entry.object_id, for_update=for_update
            )
            if (
                current is None
                or current["lifecycle"] != "active"
                or current["object_kind"] != entry.kind
            ):
                return None
        return current

    async def _resolve_parent(
        self,
        session: AsyncSession,
        scope: Scope,
        path: str,
        *,
        create: bool,
    ) -> tuple[Mapping[str, Any], tuple[_ManifestEntry, ...], str]:
        segments = path[1:].split("/")
        leaf = segments.pop()
        current = (
            await self._ensure_root(session, scope)
            if create
            else await self._load_root(session, scope, for_update=True)
        )
        if current is None:
            raise NotFoundOrDenied("memory object is unavailable")
        for segment in segments:
            manifest = await self._load_manifest(session, scope, current)
            entry = next((item for item in manifest if item.name == segment), None)
            if entry is None:
                if not create:
                    raise NotFoundOrDenied("memory object is unavailable")
                directory_id = _opaque_uuid()
                await self._insert_object(
                    session,
                    scope,
                    object_id=directory_id,
                    kind="directory",
                )
                await self._insert_version(
                    session,
                    scope,
                    object_id=directory_id,
                    kind="directory",
                    version=1,
                    raw=_encode_manifest(()),
                )
                entry = _ManifestEntry(
                    name=segment, object_id=directory_id, kind="directory"
                )
                await self._replace_manifest(
                    session, scope, current, (*manifest, entry)
                )
            if entry.kind != "directory":
                raise NotFoundOrDenied("memory object is unavailable")
            next_row = await self._load_object(
                session, scope, entry.object_id, for_update=True
            )
            if (
                next_row is None
                or next_row["lifecycle"] != "active"
                or next_row["object_kind"] != "directory"
            ):
                raise NotFoundOrDenied("memory object is unavailable")
            current = next_row
        return current, await self._load_manifest(session, scope, current), leaf

    async def _replace_manifest(
        self,
        session: AsyncSession,
        scope: Scope,
        object_row: Mapping[str, Any],
        entries: Sequence[_ManifestEntry],
    ) -> None:
        old_version = int(object_row["current_version"])
        new_version = old_version + 1
        await self._insert_version(
            session,
            scope,
            object_id=object_row["object_id"],
            kind=object_row["object_kind"],
            version=new_version,
            raw=_encode_manifest(entries),
        )
        changed = await session.execute(
            update(self.tables.objects)
            .where(
                _object_predicate(
                    self.tables.objects, scope, object_row["object_id"]
                ),
                self.tables.objects.c.current_version == old_version,
                self.tables.objects.c.lifecycle == "active",
            )
            .values(current_version=new_version, updated_at=_now())
        )
        if changed.rowcount != 1:
            raise VersionConflict("directory changed concurrently")
        await session.execute(
            update(self.tables.versions)
            .where(
                _object_predicate(
                    self.tables.versions, scope, object_row["object_id"]
                ),
                self.tables.versions.c.version == old_version,
                self.tables.versions.c.purge_after.is_(None),
            )
            .values(purge_after=_now() + self.config.retention_window)
        )
        # RowMapping is immutable, but callers may keep traversing this object.
        if isinstance(object_row, dict):
            object_row["current_version"] = new_version
            object_row["updated_at"] = _now()

    async def _write_new_version(
        self,
        session: AsyncSession,
        scope: Scope,
        path: str,
        raw: bytes,
        *,
        expected_version: int | None,
    ) -> tuple[WriteResult, str]:
        parent, manifest, leaf = await self._resolve_parent(
            session, scope, path, create=True
        )
        entry = next((item for item in manifest if item.name == leaf), None)
        if entry is None:
            if expected_version is not None:
                raise VersionConflict("document does not have expected version")
            await self._change_quota(
                session,
                scope,
                bytes_delta=len(raw),
                documents_delta=1,
            )
            object_id = _opaque_uuid()
            await self._insert_object(
                session,
                scope,
                object_id=object_id,
                kind="document",
                logical_bytes=len(raw),
            )
            await self._insert_version(
                session,
                scope,
                object_id=object_id,
                kind="document",
                version=1,
                raw=raw,
            )
            await self._replace_manifest(
                session,
                scope,
                parent,
                (*manifest, _ManifestEntry(leaf, object_id, "document")),
            )
            return WriteResult(path=path, version=1, created=True), object_id
        resolved = await self._load_object(
            session, scope, entry.object_id, for_update=True
        )
        if (
            resolved is None
            or resolved["lifecycle"] != "active"
            or resolved["object_kind"] != "document"
            or entry.kind != "document"
        ):
            raise NotFoundOrDenied("memory object is unavailable")
        if expected_version is None or resolved["current_version"] != expected_version:
            raise VersionConflict("document version does not match")
        return (
            await self._replace_document(session, scope, path, resolved, raw),
            resolved["object_id"],
        )

    async def _replace_document(
        self,
        session: AsyncSession,
        scope: Scope,
        path: str,
        object_row: Mapping[str, Any],
        raw: bytes,
    ) -> WriteResult:
        old_version = int(object_row["current_version"])
        new_version = old_version + 1
        delta = len(raw) - int(object_row["logical_bytes"])
        await self._change_quota(
            session, scope, bytes_delta=delta, documents_delta=0
        )
        await self._insert_version(
            session,
            scope,
            object_id=object_row["object_id"],
            kind="document",
            version=new_version,
            raw=raw,
        )
        changed = await session.execute(
            update(self.tables.objects)
            .where(
                _object_predicate(
                    self.tables.objects, scope, object_row["object_id"]
                ),
                self.tables.objects.c.current_version == old_version,
                self.tables.objects.c.lifecycle == "active",
            )
            .values(
                current_version=new_version,
                logical_bytes=len(raw),
                updated_at=_now(),
            )
        )
        if changed.rowcount != 1:
            raise VersionConflict("document changed concurrently")
        await session.execute(
            update(self.tables.versions)
            .where(
                _object_predicate(
                    self.tables.versions, scope, object_row["object_id"]
                ),
                self.tables.versions.c.version == old_version,
                self.tables.versions.c.purge_after.is_(None),
            )
            .values(purge_after=_now() + self.config.retention_window)
        )
        return WriteResult(path=path, version=new_version, created=False)

    async def _change_quota(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        bytes_delta: int,
        documents_delta: int,
        physical_objects_delta: int = 0,
    ) -> None:
        table = self.tables.quotas
        await session.execute(
            pg_insert(table)
            .values(
                **_scope_values(scope),
                logical_bytes=0,
                document_count=0,
                physical_object_count=0,
                updated_at=_now(),
            )
            .on_conflict_do_nothing()
        )
        quota = (
            await session.execute(
                select(table)
                .where(_scope_predicate(table, scope))
                .with_for_update()
            )
        ).mappings().one()
        new_bytes = int(quota["logical_bytes"]) + bytes_delta
        new_documents = int(quota["document_count"]) + documents_delta
        new_physical_objects = (
            int(quota["physical_object_count"]) + physical_objects_delta
        )
        if (
            new_bytes < 0
            or new_documents < 0
            or new_physical_objects < 0
            or new_bytes > self.config.max_scope_bytes
            or new_documents > self.config.max_documents
            or new_physical_objects > self.config.max_physical_objects
        ):
            raise QuotaExceeded("scope quota would be exceeded")
        await session.execute(
            update(table)
            .where(_scope_predicate(table, scope))
            .values(
                logical_bytes=new_bytes,
                document_count=new_documents,
                physical_object_count=new_physical_objects,
                updated_at=_now(),
            )
        )

    async def _find_idempotency(
        self,
        session: AsyncSession,
        scope: Scope,
        key: str,
        fingerprint: str,
    ) -> _IdempotencyReplay | None:
        lookup_digest = self._idempotency_digest(scope, key)
        row = (
            await session.execute(
                select(self.tables.idempotency)
                .where(
                    _scope_predicate(self.tables.idempotency, scope),
                    self.tables.idempotency.c.lookup_digest == lookup_digest,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        if row["expires_at"] <= _now():
            await session.execute(
                delete(self.tables.idempotency).where(
                    _scope_predicate(self.tables.idempotency, scope),
                    self.tables.idempotency.c.lookup_digest == lookup_digest,
                )
            )
            return None
        raw = await self._decrypt_row(
            row,
            scope,
            purpose="memory-idempotency",
            object_id=row["record_id"],
            object_kind="idempotency",
            version=1,
        )
        try:
            body = json.loads(raw.decode("utf-8"))
            if (
                body.get("format") != 1
                or not isinstance(body.get("key"), str)
                or not isinstance(body.get("fingerprint"), str)
                or not isinstance(body.get("result"), dict)
            ):
                raise ValueError
        except Exception as exc:
            raise IntegrityFailure("encrypted idempotency record is malformed") from exc
        if not hmac.compare_digest(body["key"], key):
            raise IntegrityFailure("encrypted idempotency record is malformed")
        if not hmac.compare_digest(body["fingerprint"], fingerprint):
            raise IdempotencyConflict(
                "idempotency key was used for a different request"
            )
        return _IdempotencyReplay(
            fingerprint=body["fingerprint"], result=body["result"]
        )

    def _idempotency_digest(self, scope: Scope, key: str) -> str:
        # Idempotency keys are already opaque high-entropy caller identifiers.
        # Scope and namespace domain-separate the non-reversible direct index.
        encoded = "\x1f".join(
            (
                "idempotency-v1",
                self.config.service_namespace,
                *_scope_values(scope).values(),
                key,
            )
        ).encode("utf-8")
        return hmac.new(
            self.config.idempotency_index_key, encoded, hashlib.sha256
        ).hexdigest()

    async def _store_idempotency(
        self,
        session: AsyncSession,
        scope: Scope,
        key: str,
        fingerprint: str,
        result: Mapping[str, Any],
    ) -> None:
        record_id = _opaque_uuid()
        raw = json.dumps(
            {
                "format": 1,
                "fingerprint": fingerprint,
                "key": key,
                "result": result,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload = await self._encrypt(
            raw,
            scope,
            purpose="memory-idempotency",
            object_id=record_id,
            object_kind="idempotency",
            version=1,
        )
        now = _now()
        await session.execute(
            insert(self.tables.idempotency).values(
                **_scope_values(scope),
                record_id=record_id,
                lookup_digest=self._idempotency_digest(scope, key),
                ciphertext=payload.ciphertext,
                wrapped_dek=payload.wrapped_dek,
                provider_id=payload.provider_id,
                key_id=payload.key_id,
                format_version=payload.format_version,
                created_at=now,
                expires_at=now + self.config.idempotency_ttl,
            )
        )

    async def _export_object(
        self,
        session: AsyncSession,
        scope: Scope,
        path: str,
        object_row: Mapping[str, Any],
        snapshots: list[DocumentSnapshot],
    ) -> None:
        if object_row["object_kind"] == "document":
            raw, version_created = await self._load_current_payload(
                session, scope, object_row
            )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise IntegrityFailure("encrypted document is malformed") from exc
            snapshots.append(
                DocumentSnapshot(
                    path=path,
                    content=content,
                    version=object_row["current_version"],
                    created_at=object_row["created_at"],
                    updated_at=version_created,
                )
            )
            return
        manifest = await self._load_manifest(session, scope, object_row)
        for entry in manifest:
            child = await self._load_object(session, scope, entry.object_id)
            if (
                child is None
                or child["lifecycle"] != "active"
                or child["object_kind"] != entry.kind
            ):
                raise IntegrityFailure("encrypted manifest target is invalid")
            child_path = "/" + entry.name if path == "/" else path + "/" + entry.name
            await self._export_object(session, scope, child_path, child, snapshots)

    async def _audit(
        self,
        session: AsyncSession,
        scope: Scope,
        action: str,
        outcome: str,
        *,
        principal_id: str | None = None,
        invocation_id: str | None = None,
        object_id: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        occurred_at = _now()
        await session.execute(
            insert(self.tables.audit_events).values(
                **_scope_values(scope),
                event_id=_opaque_uuid(),
                principal_id=principal_id,
                invocation_id=invocation_id,
                object_id=object_id,
                action=action,
                outcome=outcome,
                reason_code=reason_code,
                occurred_at=occurred_at,
                expires_at=occurred_at + self.config.audit_retention_window,
            )
        )

    async def _audit_failure(
        self,
        scope: Scope,
        action: str,
        exc: Exception,
        *,
        principal_id: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        outcome, reason = _safe_failure(exc)
        try:
            async with self.runtime.session() as session, session.begin():
                await self._audit(
                    session,
                    scope,
                    action,
                    outcome,
                    principal_id=principal_id,
                    invocation_id=invocation_id,
                    reason_code=reason,
                )
        except Exception:
            # Storage failure must never replace or disclose the primary error.
            return


__all__ = ["EnvelopeCodec", "PostgresMemoryStore", "PostgresStoreConfig"]
