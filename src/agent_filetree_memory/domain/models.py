"""Immutable domain values shared by every adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re

from .errors import AuthorizationDenied

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:-]{0,254}$")


def validate_opaque_id(value: str, *, field: str) -> str:
    """Reject values that look like paths, free text, or empty identifiers."""
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise ValueError(f"{field} must be a non-empty opaque identifier")
    return value


class MemoryAction(StrEnum):
    LIST = "memory:list"
    READ = "memory:read"
    WRITE = "memory:write"
    APPEND = "memory:append"
    DELETE = "memory:delete"
    EXPORT = "memory:export"
    IMPORT = "memory:import"


@dataclass(frozen=True, slots=True)
class Scope:
    workspace_id: str
    agent_profile_id: str

    def __post_init__(self) -> None:
        for field in (
            "workspace_id",
            "agent_profile_id",
        ):
            validate_opaque_id(getattr(self, field), field=field)


@dataclass(frozen=True, slots=True)
class VerifiedInvocation:
    scope: Scope
    principal_id: str
    invocation_id: str
    capability_id: str
    issuer: str
    audience: str
    allowed_actions: frozenset[MemoryAction]
    issued_at: datetime
    expires_at: datetime
    delegation_depth: int = 0

    def __post_init__(self) -> None:
        try:
            immutable_actions = frozenset(MemoryAction(action) for action in self.allowed_actions)
        except (TypeError, ValueError) as exc:
            raise ValueError("allowed_actions contains an unsupported action") from exc
        object.__setattr__(self, "allowed_actions", immutable_actions)
        validate_opaque_id(self.principal_id, field="principal_id")
        validate_opaque_id(self.invocation_id, field="invocation_id")
        validate_opaque_id(self.capability_id, field="capability_id")
        if not self.issuer or not self.audience:
            raise ValueError("issuer and audience are required")
        if not immutable_actions:
            raise ValueError("allowed_actions cannot be empty")
        if self.delegation_depth < 0:
            raise ValueError("delegation_depth cannot be negative")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("capability timestamps must be timezone-aware")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must follow issued_at")

    def require(self, action: MemoryAction, *, now: datetime | None = None) -> None:
        checked_at = now or datetime.now(timezone.utc)
        if checked_at >= self.expires_at or action not in self.allowed_actions:
            raise AuthorizationDenied("memory operation is not authorized")


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    name: str
    path: str
    kind: str
    version: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    path: str
    content: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: str
    version: int
    created: bool
    idempotent_replay: bool = False


@dataclass(frozen=True, slots=True)
class DeleteResult:
    path: str
    deleted_version: int
    purge_after: datetime
    idempotent_replay: bool = False
