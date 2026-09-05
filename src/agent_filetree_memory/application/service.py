"""Authorization-first use cases for the memory tree."""

from __future__ import annotations

import logging
import hashlib
import re
from collections.abc import Mapping
from typing import Sequence

from ..domain.errors import AuthorizationDenied
from ..domain.models import (
    DeleteResult,
    DocumentSnapshot,
    HistoricalDocument,
    MemoryAction,
    MemoryEntry,
    MemoryHistoryPage,
    VerifiedInvocation,
    WriteResult,
)
from ..domain.paths import normalize_memory_path
from ..ports.store import MemoryStore

_DEFAULT_MAX_CONTENT_BYTES = 1024 * 1024
_DEFAULT_MAX_APPEND_BYTES = 256 * 1024
_DEFAULT_MAX_IMPORT_DOCUMENTS = 1_000
_DEFAULT_MAX_HISTORY_PAGE_SIZE = 100
_DEFAULT_MAX_CO_AUTHORS = 8
_DEFAULT_MAX_CHANGE_COMMENT_BYTES = 2_048
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{0,254}$")


class MemoryService:
    """Perform memory operations within one already-verified invocation.

    The invocation is authorized before request validation and, critically,
    before the persistence port is called.  A transport adapter must never
    accept scope fields from a model or UI and construct an invocation itself.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        max_content_bytes: int = _DEFAULT_MAX_CONTENT_BYTES,
        max_append_bytes: int = _DEFAULT_MAX_APPEND_BYTES,
        max_import_documents: int = _DEFAULT_MAX_IMPORT_DOCUMENTS,
        max_history_page_size: int = _DEFAULT_MAX_HISTORY_PAGE_SIZE,
        max_co_authors: int = _DEFAULT_MAX_CO_AUTHORS,
        max_change_comment_bytes: int = _DEFAULT_MAX_CHANGE_COMMENT_BYTES,
        logger: logging.Logger | None = None,
    ) -> None:
        if not isinstance(max_content_bytes, int) or isinstance(
            max_content_bytes, bool
        ):
            raise ValueError("max_content_bytes must be a positive integer")
        if not isinstance(max_append_bytes, int) or isinstance(max_append_bytes, bool):
            raise ValueError("max_append_bytes must be a positive integer")
        if max_content_bytes <= 0 or max_append_bytes <= 0:
            raise ValueError("content limits must be positive")
        if max_append_bytes > max_content_bytes:
            raise ValueError("append limit cannot exceed the document limit")
        if (
            not isinstance(max_import_documents, int)
            or isinstance(max_import_documents, bool)
            or max_import_documents <= 0
        ):
            raise ValueError("max_import_documents must be a positive integer")
        for name, value in (
            ("max_history_page_size", max_history_page_size),
            ("max_co_authors", max_co_authors),
            ("max_change_comment_bytes", max_change_comment_bytes),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        self._store = store
        self._max_content_bytes = max_content_bytes
        self._max_append_bytes = max_append_bytes
        self._max_import_documents = max_import_documents
        self._max_history_page_size = max_history_page_size
        self._max_co_authors = max_co_authors
        self._max_change_comment_bytes = max_change_comment_bytes
        self._logger = logger or logging.getLogger(__name__)

    async def list(
        self, invocation: VerifiedInvocation, path: str = "/"
    ) -> Sequence[MemoryEntry]:
        self._authorize(invocation, MemoryAction.LIST)
        normalized = normalize_memory_path(path)
        result = await self._store.list(
            invocation.scope,
            normalized,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
        )
        self._completed(MemoryAction.LIST)
        return result

    async def read(
        self, invocation: VerifiedInvocation, path: str
    ) -> DocumentSnapshot:
        self._authorize(invocation, MemoryAction.READ)
        normalized = normalize_memory_path(path, allow_root=False)
        result = await self._store.read(
            invocation.scope,
            normalized,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
        )
        self._completed(MemoryAction.READ)
        return result

    async def list_history(
        self,
        invocation: VerifiedInvocation,
        path: str,
        *,
        limit: int = 20,
        before_version: int | None = None,
    ) -> MemoryHistoryPage:
        self._authorize(invocation, MemoryAction.HISTORY_LIST)
        normalized = normalize_memory_path(path, allow_root=False)
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= self._max_history_page_size
        ):
            raise ValueError("history limit is outside the configured range")
        if before_version is not None:
            self._validate_expected_version(before_version, allow_none=False)
        result = await self._store.list_history(
            invocation.scope,
            normalized,
            limit=limit,
            before_version=before_version,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
        )
        self._completed(MemoryAction.HISTORY_LIST)
        return result

    async def read_history(
        self,
        invocation: VerifiedInvocation,
        path: str,
        version: int,
        *,
        compare_to_version: int | None = None,
    ) -> HistoricalDocument:
        self._authorize(invocation, MemoryAction.HISTORY_READ)
        normalized = normalize_memory_path(path, allow_root=False)
        self._validate_expected_version(version, allow_none=False)
        if compare_to_version is not None:
            self._validate_expected_version(compare_to_version, allow_none=False)
        result = await self._store.read_history(
            invocation.scope,
            normalized,
            version,
            compare_to_version=compare_to_version,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
        )
        self._completed(MemoryAction.HISTORY_READ)
        return result

    async def write(
        self,
        invocation: VerifiedInvocation,
        path: str,
        content: str,
        *,
        expected_version: int | None = None,
        idempotency_key: str,
        co_authored_by: Sequence[str] = (),
        change_comment: str | None = None,
    ) -> WriteResult:
        self._authorize(invocation, MemoryAction.WRITE)
        normalized = normalize_memory_path(path, allow_root=False)
        validated_content = self._validate_content(
            content, max_bytes=self._max_content_bytes, allow_empty=True
        )
        self._validate_expected_version(expected_version, allow_none=True)
        validated_key = self._validate_idempotency_key(idempotency_key)
        validated_co_authors = self._validate_co_authors(co_authored_by)
        validated_comment = self._validate_change_comment(change_comment)
        result = await self._store.write(
            invocation.scope,
            normalized,
            validated_content,
            expected_version=expected_version,
            idempotency_key=validated_key,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
            co_authored_by=validated_co_authors,
            change_comment=validated_comment,
        )
        self._completed(MemoryAction.WRITE)
        return result

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
        self._authorize(invocation, MemoryAction.APPEND)
        normalized = normalize_memory_path(path, allow_root=False)
        validated_content = self._validate_content(
            content, max_bytes=self._max_append_bytes, allow_empty=False
        )
        self._validate_expected_version(expected_version, allow_none=False)
        validated_key = self._validate_idempotency_key(idempotency_key)
        validated_co_authors = self._validate_co_authors(co_authored_by)
        validated_comment = self._validate_change_comment(change_comment)
        result = await self._store.append(
            invocation.scope,
            normalized,
            validated_content,
            expected_version=expected_version,
            idempotency_key=validated_key,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
            co_authored_by=validated_co_authors,
            change_comment=validated_comment,
        )
        self._completed(MemoryAction.APPEND)
        return result

    async def edit(
        self,
        invocation: VerifiedInvocation,
        path: str,
        old_text: str,
        new_text: str,
        *,
        expected_version: int,
        idempotency_key: str,
        replace_all: bool = False,
        co_authored_by: Sequence[str] = (),
        change_comment: str | None = None,
    ) -> WriteResult:
        self._authorize(invocation, MemoryAction.WRITE)
        self._authorize(invocation, MemoryAction.READ)
        normalized = normalize_memory_path(path, allow_root=False)
        old = self._validate_content(old_text, max_bytes=self._max_content_bytes, allow_empty=False)
        new = self._validate_content(new_text, max_bytes=self._max_content_bytes, allow_empty=True)
        if not isinstance(replace_all, bool):
            raise ValueError("replace_all must be a boolean")
        self._validate_expected_version(expected_version, allow_none=False)
        key = self._validate_idempotency_key(idempotency_key)
        authors = self._validate_co_authors(co_authored_by)
        comment = self._validate_change_comment(change_comment)
        result = await self._store.edit(
            invocation.scope, normalized, old, new, replace_all=replace_all,
            expected_version=expected_version, idempotency_key=key,
            max_content_bytes=self._max_content_bytes,
            invocation_id=invocation.invocation_id, principal_id=invocation.principal_id,
            co_authored_by=authors, change_comment=comment,
        )
        self._completed(MemoryAction.WRITE)
        return result

    async def delete(
        self,
        invocation: VerifiedInvocation,
        path: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> DeleteResult:
        self._authorize(invocation, MemoryAction.DELETE)
        normalized = normalize_memory_path(path, allow_root=False)
        self._validate_expected_version(expected_version, allow_none=False)
        validated_key = self._validate_idempotency_key(idempotency_key)
        result = await self._store.delete(
            invocation.scope,
            normalized,
            expected_version=expected_version,
            idempotency_key=validated_key,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
        )
        self._completed(MemoryAction.DELETE)
        return result

    async def export(
        self, invocation: VerifiedInvocation, path: str = "/"
    ) -> Sequence[DocumentSnapshot]:
        self._authorize(invocation, MemoryAction.EXPORT)
        normalized = normalize_memory_path(path)
        result = await self._store.export_markdown_tree(
            invocation.scope,
            normalized,
            invocation_id=invocation.invocation_id,
            principal_id=invocation.principal_id,
        )
        self._completed(MemoryAction.EXPORT)
        return result

    async def import_markdown_tree(
        self,
        invocation: VerifiedInvocation,
        documents: Mapping[str, str],
        *,
        idempotency_namespace: str,
    ) -> Sequence[WriteResult]:
        """Create a portable Markdown tree through retry-safe package writes.

        The import is intentionally create-only. Every path/content pair is
        normalized and validated before the first database call. Each document
        then receives a stable, opaque idempotency key derived from the caller's
        namespace and normalized path, so an interrupted import can be retried
        safely. Hosts that need an all-or-nothing bulk transaction should build
        that policy above the persistence port.
        """

        self._authorize(invocation, MemoryAction.IMPORT)
        namespace = self._validate_idempotency_key(idempotency_namespace)
        if not isinstance(documents, Mapping) or not documents:
            raise ValueError("documents must be a non-empty path-to-content mapping")
        if len(documents) > self._max_import_documents:
            raise ValueError("documents exceed the configured import limit")

        validated: dict[str, str] = {}
        for path, content in documents.items():
            normalized = normalize_memory_path(path, allow_root=False)
            if normalized in validated:
                raise ValueError("documents contain duplicate normalized paths")
            validated[normalized] = self._validate_content(
                content,
                max_bytes=self._max_content_bytes,
                allow_empty=True,
            )

        results: list[WriteResult] = []
        for path in sorted(validated):
            digest = hashlib.sha256(
                b"agent-filetree-memory-import-v1\x00"
                + namespace.encode("ascii")
                + b"\x00"
                + path.encode("utf-8")
            ).hexdigest()
            results.append(
                await self._store.write(
                    invocation.scope,
                    path,
                    validated[path],
                    expected_version=None,
                    idempotency_key=digest,
                    invocation_id=invocation.invocation_id,
                    principal_id=invocation.principal_id,
                )
            )
        self._completed(MemoryAction.IMPORT)
        return tuple(results)

    @staticmethod
    def _authorize(
        invocation: VerifiedInvocation, required_action: MemoryAction
    ) -> None:
        if not isinstance(invocation, VerifiedInvocation) or not isinstance(
            invocation.allowed_actions, frozenset
        ):
            raise AuthorizationDenied("memory operation is not authorized")
        invocation.require(required_action)

    @staticmethod
    def _validate_content(
        content: str, *, max_bytes: int, allow_empty: bool
    ) -> str:
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("content must be valid text within configured limits")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "content must be valid text within configured limits"
            ) from None
        if (not allow_empty and not encoded) or len(encoded) > max_bytes:
            raise ValueError("content must be valid text within configured limits")
        return content

    @staticmethod
    def _validate_expected_version(
        version: int | None, *, allow_none: bool
    ) -> None:
        if version is None and allow_none:
            return
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("expected_version must be a positive integer")

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
            raise ValueError("idempotency_key must be an opaque identifier")
        return value

    def _validate_co_authors(self, values: Sequence[str]) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError("co_authored_by must contain opaque identifiers")
        if len(values) > self._max_co_authors:
            raise ValueError("co_authored_by exceeds the configured limit")
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
                raise ValueError("co_authored_by must contain opaque identifiers")
            if value in seen:
                raise ValueError("co_authored_by contains duplicate identifiers")
            result.append(value)
            seen.add(value)
        return tuple(result)

    def _validate_change_comment(self, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("change_comment must be valid non-empty text")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("change_comment must be valid non-empty text") from None
        if len(encoded) > self._max_change_comment_bytes:
            raise ValueError("change_comment exceeds the configured limit")
        return value

    def _completed(self, action: MemoryAction) -> None:
        # Deliberately omit paths, scopes, idempotency keys, and document data.
        self._logger.info(
            "memory operation completed", extra={"memory_action": action.value}
        )
