"""PostgreSQL-backed workspace, agent-profile, and grant resolution.

This module is the hosted identity boundary for agent memory. Human-readable
slugs are routing aliases only; callers receive immutable opaque identifiers
after membership and action authorization have succeeded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from uuid import uuid4

from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import (
    MemoryAction,
    Scope,
    validate_opaque_id,
)
from agent_filetree_memory.postgres import SessionFactory
from agent_filetree_memory.postgres.schema import validate_schema_name
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

DEFAULT_SCHEMA = "agent_filetree_memory"
_AUTHORIZATION_DENIED = "memory operation is not authorized"
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DEFAULT_INTEGRITY_SERVICE_NAMESPACE = "agent-filetree-memory"
_DEFAULT_CONSTRAINT_NAMESPACE = "afm"
_INTEGRITY_VERSION = 1
_INTEGRITY_RECORD_FIELDS = {
    "principal_profile": (
        "principal_id",
        "email",
        "display_name",
    ),
    "workspace": (
        "workspace_id",
        "slug",
        "created_by_principal_id",
    ),
    "workspace_member": ("workspace_id", "principal_id", "role"),
    "agent_profile": (
        "workspace_id",
        "agent_profile_id",
        "slug",
        "display_alias",
        "created_by_principal_id",
    ),
    "agent_grant": (
        "workspace_id",
        "agent_profile_id",
        "principal_id",
        "role",
    ),
    "workspace_invitation": (
        "invitation_id",
        "workspace_id",
        "email",
        "role",
        "invited_by_principal_id",
    ),
    "agent_manager": (
        "workspace_id",
        "agent_profile_id",
        "principal_id",
    ),
    "management_audit": (
        "event_id",
        "workspace_id",
        "actor_principal_id",
        "action",
        "target_kind",
        "target_id",
    ),
}
_INTEGRITY_RECORD_DOMAINS = {
    "principal_profile": "principal-profile/v1",
    "workspace": "workspace/v1",
    "workspace_member": "workspace-member/v1",
    "agent_profile": "agent-profile/v1",
    "agent_grant": "agent-grant/v1",
    "workspace_invitation": "workspace-invitation/v1",
    "agent_manager": "agent-manager/v1",
    "management_audit": "management-audit/v1",
}
_DEFAULT_MAX_WORKSPACES_PER_PRINCIPAL = 10
_DEFAULT_MAX_AGENTS_PER_WORKSPACE = 100


class WorkspaceRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class AgentGrantRole(StrEnum):
    READER = "reader"
    EDITOR = "editor"
    ADMIN = "admin"


_READER_ACTIONS = frozenset({MemoryAction.LIST, MemoryAction.READ})
_EDITOR_ACTIONS = _READER_ACTIONS | frozenset(
    {MemoryAction.WRITE, MemoryAction.APPEND}
)
_ADMIN_ACTIONS = _EDITOR_ACTIONS | frozenset({MemoryAction.DELETE})
_ROLE_ACTIONS = {
    AgentGrantRole.READER: _READER_ACTIONS,
    AgentGrantRole.EDITOR: _EDITOR_ACTIONS,
    AgentGrantRole.ADMIN: _ADMIN_ACTIONS,
}

namespace_metadata = MetaData()

workspaces = Table(
    "workspaces",
    namespace_metadata,
    Column("workspace_id", String(32), primary_key=True),
    Column("slug", String(63), nullable=False),
    Column("created_by_principal_id", String(255), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "slug",
        name="uq_afm_workspaces_slug",
    ),
    CheckConstraint(
        "slug = lower(slug)",
        name="ck_afm_workspaces_slug_lowercase",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_workspaces_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_workspaces_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

workspace_members = Table(
    "workspace_members",
    namespace_metadata,
    Column(
        "workspace_id",
        String(32),
        ForeignKey(
            f"{DEFAULT_SCHEMA}.workspaces.workspace_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("principal_id", String(255), nullable=False),
    Column("role", String(16), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    PrimaryKeyConstraint(
        "workspace_id",
        "principal_id",
        name="pk_afm_workspace_members",
    ),
    CheckConstraint(
        "role IN ('owner', 'admin', 'member')",
        name="ck_afm_workspace_members_role",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_workspace_members_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_workspace_members_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

agent_profiles = Table(
    "agent_profiles",
    namespace_metadata,
    Column("agent_profile_id", String(32), primary_key=True),
    Column(
        "workspace_id",
        String(32),
        ForeignKey(
            f"{DEFAULT_SCHEMA}.workspaces.workspace_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("slug", String(63), nullable=False),
    Column("display_alias", String(128), nullable=False),
    Column("created_by_principal_id", String(255), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "workspace_id",
        "slug",
        name="uq_afm_agent_profiles_workspace_slug",
    ),
    UniqueConstraint(
        "workspace_id",
        "agent_profile_id",
        name="uq_afm_agent_profiles_workspace_id",
    ),
    CheckConstraint(
        "slug = lower(slug)",
        name="ck_afm_agent_profiles_slug_lowercase",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_agent_profiles_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_agent_profiles_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

agent_grants = Table(
    "agent_grants",
    namespace_metadata,
    Column("workspace_id", String(32), nullable=False),
    Column("agent_profile_id", String(32), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("role", String(16), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    PrimaryKeyConstraint(
        "agent_profile_id",
        "principal_id",
        name="pk_afm_agent_grants",
    ),
    ForeignKeyConstraint(
        ["workspace_id", "agent_profile_id"],
        [
            f"{DEFAULT_SCHEMA}.agent_profiles.workspace_id",
            f"{DEFAULT_SCHEMA}.agent_profiles.agent_profile_id",
        ],
        ondelete="CASCADE",
        name="fk_afm_agent_grants_profile",
    ),
    CheckConstraint(
        "role IN ('reader', 'editor', 'admin')",
        name="ck_afm_agent_grants_role",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_agent_grants_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_agent_grants_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

principal_profiles = Table(
    "principal_profiles",
    namespace_metadata,
    Column("principal_id", String(255), primary_key=True),
    Column("email", String(254), nullable=False),
    Column("display_name", String(128), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "email",
        name="uq_afm_principal_profiles_email",
    ),
    CheckConstraint(
        "email = lower(email)",
        name="ck_afm_principal_profiles_email_lowercase",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_principal_profiles_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_principal_profiles_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

workspace_invitations = Table(
    "workspace_invitations",
    namespace_metadata,
    Column("invitation_id", String(32), primary_key=True),
    Column(
        "workspace_id",
        String(32),
        ForeignKey(
            f"{DEFAULT_SCHEMA}.workspaces.workspace_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("email", String(254), nullable=False),
    Column("role", String(16), nullable=False),
    Column("invited_by_principal_id", String(255), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    UniqueConstraint(
        "workspace_id",
        "email",
        name="uq_afm_workspace_invitations_email",
    ),
    CheckConstraint(
        "email = lower(email)",
        name="ck_afm_workspace_invitations_email_lowercase",
    ),
    CheckConstraint(
        "role IN ('admin', 'member')",
        name="ck_afm_workspace_invitations_role",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_workspace_invitations_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_workspace_invitations_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

agent_managers = Table(
    "agent_managers",
    namespace_metadata,
    Column("workspace_id", String(32), nullable=False),
    Column("agent_profile_id", String(32), nullable=False),
    Column("principal_id", String(255), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    PrimaryKeyConstraint(
        "agent_profile_id",
        "principal_id",
        name="pk_afm_agent_managers",
    ),
    ForeignKeyConstraint(
        ["workspace_id", "agent_profile_id"],
        [
            f"{DEFAULT_SCHEMA}.agent_profiles.workspace_id",
            f"{DEFAULT_SCHEMA}.agent_profiles.agent_profile_id",
        ],
        ondelete="CASCADE",
        name="fk_afm_agent_managers_profile",
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_agent_managers_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name="ck_afm_agent_managers_integrity_tag_length",
    ),
    schema=DEFAULT_SCHEMA,
)

management_audit_events = Table(
    "management_audit_events",
    namespace_metadata,
    Column("event_id", String(32), primary_key=True),
    Column(
        "workspace_id",
        String(32),
        ForeignKey(
            f"{DEFAULT_SCHEMA}.workspaces.workspace_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("actor_principal_id", String(255), nullable=False),
    Column("action", String(64), nullable=False),
    Column("target_kind", String(32), nullable=False),
    Column("target_id", String(255), nullable=False),
    Column("integrity_version", SmallInteger, nullable=False),
    Column("integrity_tag", LargeBinary(32), nullable=False),
    Column(
        "occurred_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(
        "integrity_version = 1",
        name="ck_afm_management_audit_events_integrity_version",
    ),
    CheckConstraint(
        "octet_length(integrity_tag) = 32",
        name=(
            "ck_afm_management_audit_events_integrity_tag_length"
        ),
    ),
    schema=DEFAULT_SCHEMA,
)
Index(
    "ix_afm_management_audit_workspace_time",
    management_audit_events.c.workspace_id,
    management_audit_events.c.occurred_at,
)


@dataclass(frozen=True, slots=True)
class NamespaceTables:
    """Control-plane tables bound to one deployer-selected schema."""

    metadata: MetaData
    workspaces: Table
    workspace_members: Table
    agent_profiles: Table
    agent_grants: Table
    principal_profiles: Table
    workspace_invitations: Table
    agent_managers: Table
    management_audit_events: Table


_DEFAULT_TABLES = NamespaceTables(
    metadata=namespace_metadata,
    workspaces=workspaces,
    workspace_members=workspace_members,
    agent_profiles=agent_profiles,
    agent_grants=agent_grants,
    principal_profiles=principal_profiles,
    workspace_invitations=workspace_invitations,
    agent_managers=agent_managers,
    management_audit_events=management_audit_events,
)


def _validate_constraint_namespace(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,30}", value) is None:
        raise ValueError(
            "constraint_namespace must be a lowercase SQL identifier fragment"
        )
    return value


@lru_cache(maxsize=32)
def namespace_tables_for_schema(
    schema: str = DEFAULT_SCHEMA,
    *,
    constraint_namespace: str = _DEFAULT_CONSTRAINT_NAMESPACE,
) -> NamespaceTables:
    """Return isolated table metadata for one schema and naming convention."""

    schema = validate_schema_name(schema)
    constraint_namespace = _validate_constraint_namespace(constraint_namespace)
    if (
        schema == DEFAULT_SCHEMA
        and constraint_namespace == _DEFAULT_CONSTRAINT_NAMESPACE
    ):
        return _DEFAULT_TABLES

    metadata = MetaData(schema=schema)
    for table in namespace_metadata.sorted_tables:
        table.to_metadata(metadata, schema=schema)
    for table in metadata.tables.values():
        for constraint in table.constraints:
            if constraint.name:
                constraint.name = constraint.name.replace(
                    "_afm_", f"_{constraint_namespace}_"
                )
        for index in table.indexes:
            if index.name:
                index.name = index.name.replace(
                    "_afm_", f"_{constraint_namespace}_"
                )

    return NamespaceTables(
        metadata=metadata,
        workspaces=metadata.tables[f"{schema}.workspaces"],
        workspace_members=metadata.tables[f"{schema}.workspace_members"],
        agent_profiles=metadata.tables[f"{schema}.agent_profiles"],
        agent_grants=metadata.tables[f"{schema}.agent_grants"],
        principal_profiles=metadata.tables[f"{schema}.principal_profiles"],
        workspace_invitations=metadata.tables[
            f"{schema}.workspace_invitations"
        ],
        agent_managers=metadata.tables[f"{schema}.agent_managers"],
        management_audit_events=metadata.tables[
            f"{schema}.management_audit_events"
        ],
    )


@dataclass(frozen=True, slots=True)
class NamespaceBinding:
    """Authorized immutable namespace selected from human-readable aliases."""

    principal_id: str
    workspace_id: str
    agent_profile_id: str
    display_alias: str
    workspace_role: WorkspaceRole
    agent_role: AgentGrantRole

    @property
    def scope(self) -> Scope:
        return Scope(
            workspace_id=self.workspace_id,
            agent_profile_id=self.agent_profile_id,
        )


def validate_slug(value: str, *, field: str) -> str:
    """Return one conservative lowercase routing slug."""

    if not isinstance(value, str) or _SLUG.fullmatch(value) is None:
        raise ValueError(
            f"{field} must be a lowercase slug containing letters, digits, or hyphens"
        )
    return value


def _display_alias(value: str | None, *, fallback: str) -> str:
    resolved = fallback if value is None else value
    if (
        not isinstance(resolved, str)
        or not resolved.strip()
        or len(resolved) > 128
        or "\x00" in resolved
    ):
        raise ValueError("display_alias is invalid")
    return resolved.strip()


def role_allows_action(role: AgentGrantRole, action: MemoryAction) -> bool:
    """Return whether one effective agent role permits the memory action."""

    try:
        resolved_role = AgentGrantRole(role)
        resolved_action = MemoryAction(action)
    except (TypeError, ValueError):
        return False
    return resolved_action in _ROLE_ACTIONS[resolved_role]


def _require_action(role: AgentGrantRole, action: MemoryAction) -> None:
    if not role_allows_action(role, action):
        raise AuthorizationDenied(_AUTHORIZATION_DENIED)


def _validate_integrity_service_namespace(value: str) -> str:
    validate_opaque_id(value, field="integrity_service_namespace")
    return value


def derive_namespace_integrity_key(
    source_key: bytes,
    *,
    integrity_service_namespace: str = _DEFAULT_INTEGRITY_SERVICE_NAMESPACE,
) -> bytes:
    """Derive a domain-separated key for authenticating authorization rows."""

    if not isinstance(source_key, bytes) or len(source_key) < 32:
        raise ValueError("namespace integrity source key must contain 32 bytes")
    namespace = _validate_integrity_service_namespace(
        integrity_service_namespace
    )
    context = f"{namespace}\0acl-integrity-key\0v1".encode("utf-8")
    return hmac.digest(source_key, context, "sha256")


def _record_integrity_tag(
    integrity_key: bytes,
    record_type: str,
    *,
    integrity_service_namespace: str = _DEFAULT_INTEGRITY_SERVICE_NAMESPACE,
    integrity_version: int = _INTEGRITY_VERSION,
    **fields: str,
) -> bytes:
    """Authenticate the canonical fields of one authorization record."""

    expected_fields = _INTEGRITY_RECORD_FIELDS.get(record_type)
    if expected_fields is None:
        raise ValueError("unknown integrity record type")
    if integrity_version != _INTEGRITY_VERSION:
        raise ValueError("unsupported integrity version")
    if set(fields) != set(expected_fields) or any(
        not isinstance(fields[name], str) for name in expected_fields
    ):
        raise ValueError("integrity record fields do not match the schema")

    payload = json.dumps(
        {
            "domain": _INTEGRITY_RECORD_DOMAINS[record_type],
            "fields": {name: fields[name] for name in expected_fields},
            "integrity_version": integrity_version,
            "service_namespace": _validate_integrity_service_namespace(
                integrity_service_namespace
            ),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.digest(integrity_key, payload, "sha256")


def _require_record_integrity(
    *,
    integrity_key: bytes,
    record_type: str,
    integrity_service_namespace: str = _DEFAULT_INTEGRITY_SERVICE_NAMESPACE,
    integrity_version: object,
    integrity_tag: object,
    fields: dict[str, object],
) -> None:
    """Fail closed on any malformed, unsupported, or forged ACL record."""

    try:
        valid_version = (
            isinstance(integrity_version, int)
            and not isinstance(integrity_version, bool)
            and integrity_version == _INTEGRITY_VERSION
        )
        tag = bytes(integrity_tag)
        expected = _record_integrity_tag(
            integrity_key,
            record_type,
            integrity_service_namespace=integrity_service_namespace,
            integrity_version=integrity_version,
            **fields,
        )
        valid = (
            valid_version
            and len(tag) == 32
            and hmac.compare_digest(tag, expected)
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise AuthorizationDenied(_AUTHORIZATION_DENIED)


def _provisioning_lock_id(
    integrity_service_namespace: str,
    domain: str,
    value: str,
) -> int:
    namespace = _validate_integrity_service_namespace(
        integrity_service_namespace
    )
    payload = f"{namespace}\0{domain}\0{value}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


async def _acquire_provisioning_lock(
    session: Any,
    *,
    integrity_service_namespace: str = _DEFAULT_INTEGRITY_SERVICE_NAMESPACE,
    domain: str,
    value: str,
) -> None:
    """Serialize bounded namespace creation across service replicas."""

    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                _provisioning_lock_id(
                    integrity_service_namespace,
                    domain,
                    value,
                )
            )
        )
    )


class NamespaceStore:
    """Resolve or atomically create authorized workspace/agent aliases."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        integrity_key: bytes,
        tables: NamespaceTables | None = None,
        integrity_service_namespace: str = (
            _DEFAULT_INTEGRITY_SERVICE_NAMESPACE
        ),
        max_workspaces_per_principal: int = (
            _DEFAULT_MAX_WORKSPACES_PER_PRINCIPAL
        ),
        max_agents_per_workspace: int = _DEFAULT_MAX_AGENTS_PER_WORKSPACE,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if not isinstance(integrity_key, bytes) or len(integrity_key) < 32:
            raise ValueError("integrity_key must contain at least 32 bytes")
        for name, value in (
            ("max_workspaces_per_principal", max_workspaces_per_principal),
            ("max_agents_per_workspace", max_agents_per_workspace),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        self._session_factory = session_factory
        self._integrity_key = integrity_key
        self._tables = tables or namespace_tables_for_schema()
        self._integrity_service_namespace = (
            _validate_integrity_service_namespace(
                integrity_service_namespace
            )
        )
        self._max_workspaces_per_principal = max_workspaces_per_principal
        self._max_agents_per_workspace = max_agents_per_workspace

    async def resolve_or_create(
        self,
        *,
        workspace_slug: str,
        agent_slug: str,
        principal_id: str,
        action: MemoryAction,
        display_alias: str | None = None,
    ) -> NamespaceBinding:
        """Resolve stable IDs, creating only within the caller's membership.

        PostgreSQL uniqueness plus ``ON CONFLICT DO NOTHING`` makes competing
        claims deterministic: exactly one principal can claim a new workspace,
        and exactly one member becomes administrator of a concurrently created
        agent profile.
        """

        workspace_slug = validate_slug(workspace_slug, field="workspace_slug")
        agent_slug = validate_slug(agent_slug, field="agent_slug")
        validate_opaque_id(principal_id, field="principal_id")
        try:
            action = MemoryAction(action)
        except (TypeError, ValueError):
            raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None
        requested_display_alias = _display_alias(
            display_alias,
            fallback=agent_slug,
        )

        async with self._session_factory() as session, session.begin():
            await _acquire_provisioning_lock(
                session,
                integrity_service_namespace=self._integrity_service_namespace,
                domain="principal-workspaces",
                value=principal_id,
            )
            workspace_exists = (
                await session.execute(
                    select(self._tables.workspaces.c.workspace_id).where(
                        self._tables.workspaces.c.slug == workspace_slug
                    )
                )
            ).scalar_one_or_none()
            if workspace_exists is None:
                workspace_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(self._tables.workspaces)
                        .where(
                            self._tables.workspaces.c.created_by_principal_id
                            == principal_id
                        )
                    )
                ).scalar_one()
                if workspace_count >= self._max_workspaces_per_principal:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)

            workspace_id = uuid4().hex
            workspace_values = {
                "workspace_id": workspace_id,
                "slug": workspace_slug,
                "created_by_principal_id": principal_id,
            }
            created_workspace_id = (
                await session.execute(
                    pg_insert(self._tables.workspaces)
                    .values(
                        **workspace_values,
                        integrity_version=_INTEGRITY_VERSION,
                        integrity_tag=_record_integrity_tag(
                            self._integrity_key,
                            "workspace",
                            integrity_service_namespace=(
                                self._integrity_service_namespace
                            ),
                            **workspace_values,
                        ),
                    )
                    .on_conflict_do_nothing(index_elements=[self._tables.workspaces.c.slug])
                    .returning(self._tables.workspaces.c.workspace_id)
                )
            ).scalar_one_or_none()

            if created_workspace_id is not None:
                workspace_id = created_workspace_id
                workspace_role = WorkspaceRole.OWNER
                member_values = {
                    "workspace_id": workspace_id,
                    "principal_id": principal_id,
                    "role": workspace_role.value,
                }
                created_member_tag = (
                    await session.execute(
                        pg_insert(self._tables.workspace_members)
                        .values(
                            **member_values,
                            integrity_version=_INTEGRITY_VERSION,
                            integrity_tag=_record_integrity_tag(
                                self._integrity_key,
                                "workspace_member",
                                integrity_service_namespace=(
                                    self._integrity_service_namespace
                                ),
                                **member_values,
                            ),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                self._tables.workspace_members.c.workspace_id,
                                self._tables.workspace_members.c.principal_id,
                            ]
                        )
                        .returning(self._tables.workspace_members.c.integrity_tag)
                    )
                ).scalar_one_or_none()
                if created_member_tag is None:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            else:
                workspace_row = (
                    await session.execute(
                        select(
                            self._tables.workspaces.c.workspace_id,
                            self._tables.workspaces.c.slug,
                            self._tables.workspaces.c.created_by_principal_id,
                            self._tables.workspaces.c.integrity_version,
                            self._tables.workspaces.c.integrity_tag,
                        ).where(self._tables.workspaces.c.slug == workspace_slug)
                    )
                ).one_or_none()
                if workspace_row is None:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
                _require_record_integrity(
                    integrity_key=self._integrity_key,
                    integrity_service_namespace=self._integrity_service_namespace,
                    record_type="workspace",
                    integrity_version=workspace_row.integrity_version,
                    integrity_tag=workspace_row.integrity_tag,
                    fields={
                        "workspace_id": workspace_row.workspace_id,
                        "slug": workspace_row.slug,
                        "created_by_principal_id": (
                            workspace_row.created_by_principal_id
                        ),
                    },
                )
                workspace_id = workspace_row.workspace_id
                member_row = (
                    await session.execute(
                        select(
                            self._tables.workspace_members.c.role,
                            self._tables.workspace_members.c.integrity_version,
                            self._tables.workspace_members.c.integrity_tag,
                        ).where(
                            self._tables.workspace_members.c.workspace_id == workspace_id,
                            self._tables.workspace_members.c.principal_id == principal_id,
                        )
                    )
                ).one_or_none()
                if member_row is None:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
                _require_record_integrity(
                    integrity_key=self._integrity_key,
                    integrity_service_namespace=self._integrity_service_namespace,
                    record_type="workspace_member",
                    integrity_version=member_row.integrity_version,
                    integrity_tag=member_row.integrity_tag,
                    fields={
                        "workspace_id": workspace_id,
                        "principal_id": principal_id,
                        "role": member_row.role,
                    },
                )
                try:
                    workspace_role = WorkspaceRole(member_row.role)
                except (TypeError, ValueError):
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None

            agent_profile_id = uuid4().hex
            profile_values = {
                "agent_profile_id": agent_profile_id,
                "workspace_id": workspace_id,
                "slug": agent_slug,
                "display_alias": requested_display_alias,
                "created_by_principal_id": principal_id,
            }
            await _acquire_provisioning_lock(
                session,
                integrity_service_namespace=self._integrity_service_namespace,
                domain="workspace-agents",
                value=workspace_id,
            )
            agent_exists = (
                await session.execute(
                    select(self._tables.agent_profiles.c.agent_profile_id).where(
                        self._tables.agent_profiles.c.workspace_id == workspace_id,
                        self._tables.agent_profiles.c.slug == agent_slug,
                    )
                )
            ).scalar_one_or_none()
            if agent_exists is None and workspace_role in {
                WorkspaceRole.OWNER,
                WorkspaceRole.ADMIN,
            }:
                agent_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(self._tables.agent_profiles)
                        .where(self._tables.agent_profiles.c.workspace_id == workspace_id)
                    )
                ).scalar_one()
                if agent_count >= self._max_agents_per_workspace:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            created_agent_id = None
            if workspace_role in {WorkspaceRole.OWNER, WorkspaceRole.ADMIN}:
                created_agent_id = (
                    await session.execute(
                        pg_insert(self._tables.agent_profiles)
                        .values(
                            **profile_values,
                            integrity_version=_INTEGRITY_VERSION,
                            integrity_tag=_record_integrity_tag(
                                self._integrity_key,
                                "agent_profile",
                                integrity_service_namespace=(
                                    self._integrity_service_namespace
                                ),
                                **profile_values,
                            ),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                self._tables.agent_profiles.c.workspace_id,
                                self._tables.agent_profiles.c.slug,
                            ]
                        )
                        .returning(self._tables.agent_profiles.c.agent_profile_id)
                    )
                ).scalar_one_or_none()

            if created_agent_id is not None:
                agent_profile_id = created_agent_id
                stored_display_alias = requested_display_alias
                agent_role = AgentGrantRole.ADMIN
                grant_values = {
                    "workspace_id": workspace_id,
                    "agent_profile_id": agent_profile_id,
                    "principal_id": principal_id,
                    "role": agent_role.value,
                }
                created_grant_tag = (
                    await session.execute(
                        pg_insert(self._tables.agent_grants)
                        .values(
                            **grant_values,
                            integrity_version=_INTEGRITY_VERSION,
                            integrity_tag=_record_integrity_tag(
                                self._integrity_key,
                                "agent_grant",
                                integrity_service_namespace=(
                                    self._integrity_service_namespace
                                ),
                                **grant_values,
                            ),
                        )
                        .on_conflict_do_nothing(
                            index_elements=[
                                self._tables.agent_grants.c.agent_profile_id,
                                self._tables.agent_grants.c.principal_id,
                            ]
                        )
                        .returning(self._tables.agent_grants.c.integrity_tag)
                    )
                ).scalar_one_or_none()
                if created_grant_tag is None:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            else:
                agent_row = (
                    await session.execute(
                        select(
                            self._tables.agent_profiles.c.agent_profile_id,
                            self._tables.agent_profiles.c.workspace_id,
                            self._tables.agent_profiles.c.slug,
                            self._tables.agent_profiles.c.display_alias,
                            self._tables.agent_profiles.c.created_by_principal_id,
                            self._tables.agent_profiles.c.integrity_version,
                            self._tables.agent_profiles.c.integrity_tag,
                        ).where(
                            self._tables.agent_profiles.c.workspace_id == workspace_id,
                            self._tables.agent_profiles.c.slug == agent_slug,
                        )
                    )
                ).one_or_none()
                if agent_row is None:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
                _require_record_integrity(
                    integrity_key=self._integrity_key,
                    integrity_service_namespace=self._integrity_service_namespace,
                    record_type="agent_profile",
                    integrity_version=agent_row.integrity_version,
                    integrity_tag=agent_row.integrity_tag,
                    fields={
                        "agent_profile_id": agent_row.agent_profile_id,
                        "workspace_id": agent_row.workspace_id,
                        "slug": agent_row.slug,
                        "display_alias": agent_row.display_alias,
                        "created_by_principal_id": (
                            agent_row.created_by_principal_id
                        ),
                    },
                )
                agent_profile_id = agent_row.agent_profile_id
                stored_display_alias = agent_row.display_alias

                # Workspace ownership is a management role, not a memory
                # decryption grant.  Even an owner must have an explicit
                # per-agent grant before this service will resolve that
                # agent's storage scope.
                grant_row = (
                    await session.execute(
                        select(
                            self._tables.agent_grants.c.role,
                            self._tables.agent_grants.c.integrity_version,
                            self._tables.agent_grants.c.integrity_tag,
                        ).where(
                            self._tables.agent_grants.c.workspace_id == workspace_id,
                            self._tables.agent_grants.c.agent_profile_id
                            == agent_profile_id,
                            self._tables.agent_grants.c.principal_id == principal_id,
                        )
                    )
                ).one_or_none()
                if grant_row is None:
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED)
                _require_record_integrity(
                    integrity_key=self._integrity_key,
                    integrity_service_namespace=self._integrity_service_namespace,
                    record_type="agent_grant",
                    integrity_version=grant_row.integrity_version,
                    integrity_tag=grant_row.integrity_tag,
                    fields={
                        "workspace_id": workspace_id,
                        "agent_profile_id": agent_profile_id,
                        "principal_id": principal_id,
                        "role": grant_row.role,
                    },
                )
                try:
                    agent_role = AgentGrantRole(grant_row.role)
                except (TypeError, ValueError):
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None

            _require_action(agent_role, action)
            return NamespaceBinding(
                principal_id=principal_id,
                workspace_id=workspace_id,
                agent_profile_id=agent_profile_id,
                display_alias=stored_display_alias,
                workspace_role=workspace_role,
                agent_role=agent_role,
            )


__all__ = [
    "AgentGrantRole",
    "NamespaceBinding",
    "NamespaceStore",
    "NamespaceTables",
    "DEFAULT_SCHEMA",
    "WorkspaceRole",
    "agent_managers",
    "agent_grants",
    "agent_profiles",
    "derive_namespace_integrity_key",
    "management_audit_events",
    "namespace_metadata",
    "namespace_tables_for_schema",
    "principal_profiles",
    "role_allows_action",
    "validate_slug",
    "workspace_invitations",
    "workspace_members",
    "workspaces",
]
