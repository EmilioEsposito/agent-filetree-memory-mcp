"""Authorization-first management operations for hosted agent memory.

Workspace administration and memory-content access are deliberately separate
axes.  Workspace owners and administrators can inventory and manage agent
namespaces without receiving a content grant.  A content grant is always an
explicit row, including when deployment policy permits an administrator to
grant one to themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any
from uuid import uuid4

from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import validate_opaque_id
from agent_filetree_memory.postgres import SessionFactory
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .namespace_store import (
    AgentGrantRole,
    NamespaceTables,
    WorkspaceRole,
    _acquire_provisioning_lock,
    _record_integrity_tag,
    _require_record_integrity,
    namespace_tables_for_schema,
    validate_slug,
)

_AUTHORIZATION_DENIED = "memory management operation is not authorized"
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_WORKSPACE_ADMIN_ROLES = frozenset(
    {WorkspaceRole.OWNER, WorkspaceRole.ADMIN}
)


class ManagementConflict(ValueError):
    """The requested management mutation conflicts with durable state."""


class SelfGrantDisabled(AuthorizationDenied):
    """Deployment policy blocks administrator content self-grants."""


@dataclass(frozen=True, slots=True)
class PrincipalProfile:
    principal_id: str
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace_id: str
    slug: str
    role: WorkspaceRole
    agent_count: int
    member_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AgentSummary:
    agent_profile_id: str
    slug: str
    display_alias: str
    content_role: AgentGrantRole | None
    can_manage: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemberAccess:
    principal_id: str
    email: str | None
    display_name: str | None
    workspace_role: WorkspaceRole
    content_role: AgentGrantRole | None = None
    explicit_manager: bool = False


@dataclass(frozen=True, slots=True)
class InvitationSummary:
    invitation_id: str
    email: str
    role: WorkspaceRole
    invited_by_principal_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ManagementEvent:
    event_id: str
    actor_principal_id: str
    action: str
    target_kind: str
    target_id: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class _WorkspaceAccess:
    workspace_id: str
    slug: str
    role: WorkspaceRole


def normalize_email(value: str) -> str:
    """Normalize a verified or invited email without using it as identity."""

    if not isinstance(value, str):
        raise ValueError("email is invalid")
    normalized = value.strip().lower()
    if (
        not normalized
        or len(normalized) > 254
        or "\x00" in normalized
        or any(character.isspace() for character in normalized)
        or _EMAIL.fullmatch(normalized) is None
    ):
        raise ValueError("email is invalid")
    return normalized


def normalize_display_name(value: str | None, *, fallback: str) -> str:
    resolved = fallback if value is None else value.strip()
    if (
        not resolved
        or len(resolved) > 128
        or "\x00" in resolved
        or any(ord(character) < 32 for character in resolved)
    ):
        raise ValueError("display name is invalid")
    return resolved


def content_role_name(role: AgentGrantRole | None) -> str | None:
    """Use an unambiguous API/UI label for the legacy full-content role."""

    if role is None:
        return None
    if role is AgentGrantRole.ADMIN:
        return "full_access"
    return role.value


def content_role_from_name(value: str) -> AgentGrantRole:
    if value == "full_access":
        return AgentGrantRole.ADMIN
    try:
        return AgentGrantRole(value)
    except (TypeError, ValueError):
        raise ValueError("content role is invalid") from None


class ManagementStore:
    """Manage identities and ACLs without touching encrypted memory content."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        integrity_key: bytes,
        tables: NamespaceTables | None = None,
        integrity_service_namespace: str = "agent-filetree-memory",
        max_workspaces_per_principal: int = 10,
        max_agents_per_workspace: int = 100,
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
        self._integrity_service_namespace = integrity_service_namespace
        self._max_workspaces_per_principal = max_workspaces_per_principal
        self._max_agents_per_workspace = max_agents_per_workspace

    def _signed(self, record_type: str, **fields: str) -> dict[str, object]:
        return {
            **fields,
            "integrity_version": 1,
            "integrity_tag": _record_integrity_tag(
                self._integrity_key,
                record_type,
                integrity_service_namespace=(
                    self._integrity_service_namespace
                ),
                **fields,
            ),
        }

    def _verify(
        self,
        row: Any,
        record_type: str,
        **fields: object,
    ) -> None:
        _require_record_integrity(
            integrity_key=self._integrity_key,
            integrity_service_namespace=self._integrity_service_namespace,
            record_type=record_type,
            integrity_version=row.integrity_version,
            integrity_tag=row.integrity_tag,
            fields=fields,
        )

    async def _audit(
        self,
        session: Any,
        *,
        workspace_id: str,
        actor_principal_id: str,
        action: str,
        target_kind: str,
        target_id: str,
    ) -> None:
        event_id = uuid4().hex
        await session.execute(
            self._tables.management_audit_events.insert().values(
                **self._signed(
                    "management_audit",
                    event_id=event_id,
                    workspace_id=workspace_id,
                    actor_principal_id=actor_principal_id,
                    action=action,
                    target_kind=target_kind,
                    target_id=target_id,
                )
            )
        )

    async def _workspace_access(
        self,
        session: Any,
        *,
        workspace_slug: str,
        principal_id: str,
    ) -> _WorkspaceAccess:
        workspace_slug = validate_slug(
            workspace_slug,
            field="workspace_slug",
        )
        validate_opaque_id(principal_id, field="principal_id")
        workspace_row = (
            await session.execute(
                select(self._tables.workspaces).where(self._tables.workspaces.c.slug == workspace_slug)
            )
        ).one_or_none()
        if workspace_row is None:
            raise AuthorizationDenied(_AUTHORIZATION_DENIED)
        self._verify(
            workspace_row,
            "workspace",
            workspace_id=workspace_row.workspace_id,
            slug=workspace_row.slug,
            created_by_principal_id=workspace_row.created_by_principal_id,
        )
        member_row = (
            await session.execute(
                select(self._tables.workspace_members).where(
                    self._tables.workspace_members.c.workspace_id
                    == workspace_row.workspace_id,
                    self._tables.workspace_members.c.principal_id == principal_id,
                )
            )
        ).one_or_none()
        if member_row is None:
            raise AuthorizationDenied(_AUTHORIZATION_DENIED)
        self._verify(
            member_row,
            "workspace_member",
            workspace_id=member_row.workspace_id,
            principal_id=member_row.principal_id,
            role=member_row.role,
        )
        try:
            role = WorkspaceRole(member_row.role)
        except (TypeError, ValueError):
            raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None
        return _WorkspaceAccess(
            workspace_id=workspace_row.workspace_id,
            slug=workspace_row.slug,
            role=role,
        )

    async def _agent_row(
        self,
        session: Any,
        *,
        workspace_id: str,
        agent_slug: str,
    ) -> Any:
        agent_slug = validate_slug(agent_slug, field="agent_slug")
        row = (
            await session.execute(
                select(self._tables.agent_profiles).where(
                    self._tables.agent_profiles.c.workspace_id == workspace_id,
                    self._tables.agent_profiles.c.slug == agent_slug,
                )
            )
        ).one_or_none()
        if row is None:
            raise AuthorizationDenied(_AUTHORIZATION_DENIED)
        self._verify(
            row,
            "agent_profile",
            workspace_id=row.workspace_id,
            agent_profile_id=row.agent_profile_id,
            slug=row.slug,
            display_alias=row.display_alias,
            created_by_principal_id=row.created_by_principal_id,
        )
        return row

    async def _content_grant(
        self,
        session: Any,
        *,
        workspace_id: str,
        agent_profile_id: str,
        principal_id: str,
    ) -> AgentGrantRole | None:
        row = (
            await session.execute(
                select(self._tables.agent_grants).where(
                    self._tables.agent_grants.c.workspace_id == workspace_id,
                    self._tables.agent_grants.c.agent_profile_id == agent_profile_id,
                    self._tables.agent_grants.c.principal_id == principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        self._verify(
            row,
            "agent_grant",
            workspace_id=row.workspace_id,
            agent_profile_id=row.agent_profile_id,
            principal_id=row.principal_id,
            role=row.role,
        )
        try:
            return AgentGrantRole(row.role)
        except (TypeError, ValueError):
            raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None

    async def _is_explicit_manager(
        self,
        session: Any,
        *,
        workspace_id: str,
        agent_profile_id: str,
        principal_id: str,
    ) -> bool:
        row = (
            await session.execute(
                select(self._tables.agent_managers).where(
                    self._tables.agent_managers.c.workspace_id == workspace_id,
                    self._tables.agent_managers.c.agent_profile_id == agent_profile_id,
                    self._tables.agent_managers.c.principal_id == principal_id,
                )
            )
        ).one_or_none()
        if row is None:
            return False
        self._verify(
            row,
            "agent_manager",
            workspace_id=row.workspace_id,
            agent_profile_id=row.agent_profile_id,
            principal_id=row.principal_id,
        )
        return True

    async def _require_agent_manager(
        self,
        session: Any,
        *,
        access: _WorkspaceAccess,
        agent_profile_id: str,
        principal_id: str,
    ) -> None:
        if access.role in _WORKSPACE_ADMIN_ROLES:
            return
        if not await self._is_explicit_manager(
            session,
            workspace_id=access.workspace_id,
            agent_profile_id=agent_profile_id,
            principal_id=principal_id,
        ):
            raise AuthorizationDenied(_AUTHORIZATION_DENIED)

    async def register_principal(
        self,
        *,
        principal_id: str,
        email: str,
        display_name: str | None,
    ) -> PrincipalProfile:
        """Record verified display metadata and atomically claim invitations."""

        validate_opaque_id(principal_id, field="principal_id")
        normalized_email = normalize_email(email)
        normalized_name = normalize_display_name(
            display_name,
            fallback=normalized_email,
        )
        async with self._session_factory() as session, session.begin():
            await _acquire_provisioning_lock(
                session,
                integrity_service_namespace=self._integrity_service_namespace,
                domain="principal-profile",
                value=principal_id,
            )
            existing = (
                await session.execute(
                    select(self._tables.principal_profiles).where(
                        self._tables.principal_profiles.c.principal_id == principal_id
                    )
                )
            ).one_or_none()
            if existing is not None:
                self._verify(
                    existing,
                    "principal_profile",
                    principal_id=existing.principal_id,
                    email=existing.email,
                    display_name=existing.display_name,
                )
            email_owner = (
                await session.execute(
                    select(self._tables.principal_profiles).where(
                        self._tables.principal_profiles.c.email == normalized_email
                    )
                )
            ).one_or_none()
            if email_owner is not None:
                self._verify(
                    email_owner,
                    "principal_profile",
                    principal_id=email_owner.principal_id,
                    email=email_owner.email,
                    display_name=email_owner.display_name,
                )
                if email_owner.principal_id != principal_id:
                    raise ManagementConflict(
                        "email is already bound to another verified identity"
                    )
            values = self._signed(
                "principal_profile",
                principal_id=principal_id,
                email=normalized_email,
                display_name=normalized_name,
            )
            await session.execute(
                pg_insert(self._tables.principal_profiles)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[self._tables.principal_profiles.c.principal_id],
                    set_={
                        "email": normalized_email,
                        "display_name": normalized_name,
                        "integrity_version": values["integrity_version"],
                        "integrity_tag": values["integrity_tag"],
                        "updated_at": func.now(),
                    },
                )
            )

            invitations = (
                await session.execute(
                    select(self._tables.workspace_invitations).where(
                        self._tables.workspace_invitations.c.email == normalized_email
                    )
                )
            ).all()
            for invitation in invitations:
                self._verify(
                    invitation,
                    "workspace_invitation",
                    invitation_id=invitation.invitation_id,
                    workspace_id=invitation.workspace_id,
                    email=invitation.email,
                    role=invitation.role,
                    invited_by_principal_id=(
                        invitation.invited_by_principal_id
                    ),
                )
                member = (
                    await session.execute(
                        select(self._tables.workspace_members).where(
                            self._tables.workspace_members.c.workspace_id
                            == invitation.workspace_id,
                            self._tables.workspace_members.c.principal_id == principal_id,
                        )
                    )
                ).one_or_none()
                if member is None:
                    await session.execute(
                        self._tables.workspace_members.insert().values(
                            **self._signed(
                                "workspace_member",
                                workspace_id=invitation.workspace_id,
                                principal_id=principal_id,
                                role=invitation.role,
                            )
                        )
                    )
                    await self._audit(
                        session,
                        workspace_id=invitation.workspace_id,
                        actor_principal_id=principal_id,
                        action="workspace.invitation.accept",
                        target_kind="principal",
                        target_id=principal_id,
                    )
                else:
                    self._verify(
                        member,
                        "workspace_member",
                        workspace_id=member.workspace_id,
                        principal_id=member.principal_id,
                        role=member.role,
                    )
                await session.execute(
                    delete(self._tables.workspace_invitations).where(
                        self._tables.workspace_invitations.c.invitation_id
                        == invitation.invitation_id
                    )
                )
        return PrincipalProfile(
            principal_id=principal_id,
            email=normalized_email,
            display_name=normalized_name,
        )

    async def ensure_local_scope(
        self,
        *,
        principal_id: str,
        workspace_id: str,
        workspace_slug: str,
        agent_profile_id: str,
        agent_slug: str,
        display_alias: str,
    ) -> None:
        """Seed the fixed local capability scope for the companion UI."""

        for name, value in (
            ("workspace_id", workspace_id),
            ("agent_profile_id", agent_profile_id),
        ):
            validate_opaque_id(value, field=name)
            if len(value) > 32:
                raise ValueError(f"{name} is too long")
        validate_opaque_id(principal_id, field="principal_id")
        workspace_slug = validate_slug(
            workspace_slug,
            field="workspace_slug",
        )
        agent_slug = validate_slug(agent_slug, field="agent_slug")
        display_alias = normalize_display_name(
            display_alias,
            fallback=agent_slug,
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                pg_insert(self._tables.workspaces)
                .values(
                    **self._signed(
                        "workspace",
                        workspace_id=workspace_id,
                        slug=workspace_slug,
                        created_by_principal_id=principal_id,
                    )
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                pg_insert(self._tables.workspace_members)
                .values(
                    **self._signed(
                        "workspace_member",
                        workspace_id=workspace_id,
                        principal_id=principal_id,
                        role=WorkspaceRole.OWNER.value,
                    )
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                pg_insert(self._tables.agent_profiles)
                .values(
                    **self._signed(
                        "agent_profile",
                        workspace_id=workspace_id,
                        agent_profile_id=agent_profile_id,
                        slug=agent_slug,
                        display_alias=display_alias,
                        created_by_principal_id=principal_id,
                    )
                )
                .on_conflict_do_nothing()
            )
            await session.execute(
                pg_insert(self._tables.agent_grants)
                .values(
                    **self._signed(
                        "agent_grant",
                        workspace_id=workspace_id,
                        agent_profile_id=agent_profile_id,
                        principal_id=principal_id,
                        role=AgentGrantRole.ADMIN.value,
                    )
                )
                .on_conflict_do_nothing()
            )
        # Resolve through the normal integrity checks after idempotent seeding.
        async with self._session_factory() as session:
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            row = await self._agent_row(
                session,
                workspace_id=access.workspace_id,
                agent_slug=agent_slug,
            )
            if (
                access.workspace_id != workspace_id
                or row.agent_profile_id != agent_profile_id
                or await self._content_grant(
                    session,
                    workspace_id=workspace_id,
                    agent_profile_id=agent_profile_id,
                    principal_id=principal_id,
                )
                is not AgentGrantRole.ADMIN
            ):
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)

    async def create_workspace(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
    ) -> WorkspaceSummary:
        workspace_slug = validate_slug(
            workspace_slug,
            field="workspace_slug",
        )
        validate_opaque_id(principal_id, field="principal_id")
        async with self._session_factory() as session, session.begin():
            await _acquire_provisioning_lock(
                session,
                integrity_service_namespace=self._integrity_service_namespace,
                domain="principal-workspaces",
                value=principal_id,
            )
            existing = (
                await session.execute(
                    select(self._tables.workspaces.c.workspace_id).where(
                        self._tables.workspaces.c.slug == workspace_slug
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ManagementConflict("workspace slug already exists")
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(self._tables.workspaces)
                    .where(
                        self._tables.workspaces.c.created_by_principal_id == principal_id
                    )
                )
            ).scalar_one()
            if count >= self._max_workspaces_per_principal:
                raise ManagementConflict("workspace limit reached")
            workspace_id = uuid4().hex
            await session.execute(
                self._tables.workspaces.insert().values(
                    **self._signed(
                        "workspace",
                        workspace_id=workspace_id,
                        slug=workspace_slug,
                        created_by_principal_id=principal_id,
                    )
                )
            )
            await session.execute(
                self._tables.workspace_members.insert().values(
                    **self._signed(
                        "workspace_member",
                        workspace_id=workspace_id,
                        principal_id=principal_id,
                        role=WorkspaceRole.OWNER.value,
                    )
                )
            )
            await self._audit(
                session,
                workspace_id=workspace_id,
                actor_principal_id=principal_id,
                action="workspace.create",
                target_kind="workspace",
                target_id=workspace_id,
            )
        summaries = await self.list_workspaces(principal_id=principal_id)
        return next(item for item in summaries if item.slug == workspace_slug)

    async def list_workspaces(
        self,
        *,
        principal_id: str,
    ) -> tuple[WorkspaceSummary, ...]:
        validate_opaque_id(principal_id, field="principal_id")
        summaries: list[WorkspaceSummary] = []
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(self._tables.workspaces, self._tables.workspace_members.c.role).join(
                        self._tables.workspace_members,
                        self._tables.workspaces.c.workspace_id
                        == self._tables.workspace_members.c.workspace_id,
                    ).where(
                        self._tables.workspace_members.c.principal_id == principal_id
                    )
                )
            ).all()
            for row in rows:
                self._verify(
                    row,
                    "workspace",
                    workspace_id=row.workspace_id,
                    slug=row.slug,
                    created_by_principal_id=row.created_by_principal_id,
                )
                member = (
                    await session.execute(
                        select(self._tables.workspace_members).where(
                            self._tables.workspace_members.c.workspace_id
                            == row.workspace_id,
                            self._tables.workspace_members.c.principal_id == principal_id,
                        )
                    )
                ).one()
                self._verify(
                    member,
                    "workspace_member",
                    workspace_id=member.workspace_id,
                    principal_id=member.principal_id,
                    role=member.role,
                )
                try:
                    role = WorkspaceRole(member.role)
                except (TypeError, ValueError):
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None
                agent_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(self._tables.agent_profiles)
                        .where(
                            self._tables.agent_profiles.c.workspace_id == row.workspace_id
                        )
                    )
                ).scalar_one()
                member_count = (
                    await session.execute(
                        select(func.count())
                        .select_from(self._tables.workspace_members)
                        .where(
                            self._tables.workspace_members.c.workspace_id
                            == row.workspace_id
                        )
                    )
                ).scalar_one()
                summaries.append(
                    WorkspaceSummary(
                        workspace_id=row.workspace_id,
                        slug=row.slug,
                        role=role,
                        agent_count=agent_count,
                        member_count=member_count,
                        created_at=row.created_at,
                    )
                )
        return tuple(sorted(summaries, key=lambda item: item.slug))

    async def create_agent(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        agent_slug: str,
        display_alias: str | None,
    ) -> AgentSummary:
        agent_slug = validate_slug(agent_slug, field="agent_slug")
        alias = normalize_display_name(display_alias, fallback=agent_slug)
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            await _acquire_provisioning_lock(
                session,
                integrity_service_namespace=self._integrity_service_namespace,
                domain="workspace-agents",
                value=access.workspace_id,
            )
            existing = (
                await session.execute(
                    select(self._tables.agent_profiles.c.agent_profile_id).where(
                        self._tables.agent_profiles.c.workspace_id == access.workspace_id,
                        self._tables.agent_profiles.c.slug == agent_slug,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise ManagementConflict("agent slug already exists")
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(self._tables.agent_profiles)
                    .where(
                        self._tables.agent_profiles.c.workspace_id == access.workspace_id
                    )
                )
            ).scalar_one()
            if count >= self._max_agents_per_workspace:
                raise ManagementConflict("agent limit reached")
            agent_profile_id = uuid4().hex
            await session.execute(
                self._tables.agent_profiles.insert().values(
                    **self._signed(
                        "agent_profile",
                        workspace_id=access.workspace_id,
                        agent_profile_id=agent_profile_id,
                        slug=agent_slug,
                        display_alias=alias,
                        created_by_principal_id=principal_id,
                    )
                )
            )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action="agent.create",
                target_kind="agent",
                target_id=agent_profile_id,
            )
        summaries = await self.list_agents(
            principal_id=principal_id,
            workspace_slug=workspace_slug,
        )
        return next(item for item in summaries if item.slug == agent_slug)

    async def list_agents(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
    ) -> tuple[AgentSummary, ...]:
        summaries: list[AgentSummary] = []
        async with self._session_factory() as session:
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            rows = (
                await session.execute(
                    select(self._tables.agent_profiles).where(
                        self._tables.agent_profiles.c.workspace_id == access.workspace_id
                    )
                )
            ).all()
            for row in rows:
                self._verify(
                    row,
                    "agent_profile",
                    workspace_id=row.workspace_id,
                    agent_profile_id=row.agent_profile_id,
                    slug=row.slug,
                    display_alias=row.display_alias,
                    created_by_principal_id=row.created_by_principal_id,
                )
                content_role = await self._content_grant(
                    session,
                    workspace_id=access.workspace_id,
                    agent_profile_id=row.agent_profile_id,
                    principal_id=principal_id,
                )
                explicit_manager = await self._is_explicit_manager(
                    session,
                    workspace_id=access.workspace_id,
                    agent_profile_id=row.agent_profile_id,
                    principal_id=principal_id,
                )
                can_manage = (
                    access.role in _WORKSPACE_ADMIN_ROLES
                    or explicit_manager
                )
                if access.role is WorkspaceRole.MEMBER and not (
                    content_role or explicit_manager
                ):
                    continue
                summaries.append(
                    AgentSummary(
                        agent_profile_id=row.agent_profile_id,
                        slug=row.slug,
                        display_alias=row.display_alias,
                        content_role=content_role,
                        can_manage=can_manage,
                        created_at=row.created_at,
                    )
                )
        return tuple(sorted(summaries, key=lambda item: item.slug))

    async def update_agent_alias(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        agent_slug: str,
        display_alias: str,
    ) -> AgentSummary:
        alias = normalize_display_name(display_alias, fallback=agent_slug)
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            agent = await self._agent_row(
                session,
                workspace_id=access.workspace_id,
                agent_slug=agent_slug,
            )
            await self._require_agent_manager(
                session,
                access=access,
                agent_profile_id=agent.agent_profile_id,
                principal_id=principal_id,
            )
            values = self._signed(
                "agent_profile",
                workspace_id=access.workspace_id,
                agent_profile_id=agent.agent_profile_id,
                slug=agent.slug,
                display_alias=alias,
                created_by_principal_id=agent.created_by_principal_id,
            )
            await session.execute(
                update(self._tables.agent_profiles)
                .where(
                    self._tables.agent_profiles.c.agent_profile_id
                    == agent.agent_profile_id
                )
                .values(
                    display_alias=alias,
                    integrity_version=values["integrity_version"],
                    integrity_tag=values["integrity_tag"],
                )
            )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action="agent.alias.update",
                target_kind="agent",
                target_id=agent.agent_profile_id,
            )
        summaries = await self.list_agents(
            principal_id=principal_id,
            workspace_slug=workspace_slug,
        )
        return next(item for item in summaries if item.slug == agent_slug)

    async def list_members(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
    ) -> tuple[tuple[MemberAccess, ...], tuple[InvitationSummary, ...]]:
        members: list[MemberAccess] = []
        invitations: list[InvitationSummary] = []
        async with self._session_factory() as session:
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            member_rows = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id
                    )
                )
            ).all()
            for member in member_rows:
                self._verify(
                    member,
                    "workspace_member",
                    workspace_id=member.workspace_id,
                    principal_id=member.principal_id,
                    role=member.role,
                )
                try:
                    role = WorkspaceRole(member.role)
                except (TypeError, ValueError):
                    raise AuthorizationDenied(_AUTHORIZATION_DENIED) from None
                profile = (
                    await session.execute(
                        select(self._tables.principal_profiles).where(
                            self._tables.principal_profiles.c.principal_id
                            == member.principal_id
                        )
                    )
                ).one_or_none()
                if profile is not None:
                    self._verify(
                        profile,
                        "principal_profile",
                        principal_id=profile.principal_id,
                        email=profile.email,
                        display_name=profile.display_name,
                    )
                members.append(
                    MemberAccess(
                        principal_id=member.principal_id,
                        email=profile.email if profile is not None else None,
                        display_name=(
                            profile.display_name
                            if profile is not None
                            else None
                        ),
                        workspace_role=role,
                    )
                )
            invitation_rows = (
                await session.execute(
                    select(self._tables.workspace_invitations).where(
                        self._tables.workspace_invitations.c.workspace_id
                        == access.workspace_id
                    )
                )
            ).all()
            for invitation in invitation_rows:
                self._verify(
                    invitation,
                    "workspace_invitation",
                    invitation_id=invitation.invitation_id,
                    workspace_id=invitation.workspace_id,
                    email=invitation.email,
                    role=invitation.role,
                    invited_by_principal_id=(
                        invitation.invited_by_principal_id
                    ),
                )
                invitations.append(
                    InvitationSummary(
                        invitation_id=invitation.invitation_id,
                        email=invitation.email,
                        role=WorkspaceRole(invitation.role),
                        invited_by_principal_id=(
                            invitation.invited_by_principal_id
                        ),
                        created_at=invitation.created_at,
                    )
                )
        return (
            tuple(sorted(members, key=lambda item: item.email or item.principal_id)),
            tuple(sorted(invitations, key=lambda item: item.email)),
        )

    async def invite_member(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        email: str,
        role: WorkspaceRole,
    ) -> str:
        normalized_email = normalize_email(email)
        if role not in {WorkspaceRole.ADMIN, WorkspaceRole.MEMBER}:
            raise ValueError("invitation role is invalid")
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            if role is WorkspaceRole.ADMIN and access.role is not WorkspaceRole.OWNER:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            profile = (
                await session.execute(
                    select(self._tables.principal_profiles).where(
                        self._tables.principal_profiles.c.email == normalized_email
                    )
                )
            ).one_or_none()
            if profile is not None:
                self._verify(
                    profile,
                    "principal_profile",
                    principal_id=profile.principal_id,
                    email=profile.email,
                    display_name=profile.display_name,
                )
                existing_member = (
                    await session.execute(
                        select(self._tables.workspace_members).where(
                            self._tables.workspace_members.c.workspace_id
                            == access.workspace_id,
                            self._tables.workspace_members.c.principal_id
                            == profile.principal_id,
                        )
                    )
                ).one_or_none()
                if existing_member is not None:
                    self._verify(
                        existing_member,
                        "workspace_member",
                        workspace_id=existing_member.workspace_id,
                        principal_id=existing_member.principal_id,
                        role=existing_member.role,
                    )
                    raise ManagementConflict("user is already a workspace member")
                await session.execute(
                    self._tables.workspace_members.insert().values(
                        **self._signed(
                            "workspace_member",
                            workspace_id=access.workspace_id,
                            principal_id=profile.principal_id,
                            role=role.value,
                        )
                    )
                )
                target_id = profile.principal_id
                action = "workspace.member.add"
            else:
                existing_invitation = (
                    await session.execute(
                        select(self._tables.workspace_invitations).where(
                            self._tables.workspace_invitations.c.workspace_id
                            == access.workspace_id,
                            self._tables.workspace_invitations.c.email == normalized_email,
                        )
                    )
                ).one_or_none()
                if existing_invitation is not None:
                    self._verify(
                        existing_invitation,
                        "workspace_invitation",
                        invitation_id=existing_invitation.invitation_id,
                        workspace_id=existing_invitation.workspace_id,
                        email=existing_invitation.email,
                        role=existing_invitation.role,
                        invited_by_principal_id=(
                            existing_invitation.invited_by_principal_id
                        ),
                    )
                    raise ManagementConflict("invitation already exists")
                invitation_id = uuid4().hex
                await session.execute(
                    self._tables.workspace_invitations.insert().values(
                        **self._signed(
                            "workspace_invitation",
                            invitation_id=invitation_id,
                            workspace_id=access.workspace_id,
                            email=normalized_email,
                            role=role.value,
                            invited_by_principal_id=principal_id,
                        )
                    )
                )
                target_id = normalized_email
                action = "workspace.invitation.create"
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action=action,
                target_kind="principal",
                target_id=target_id,
            )
        return "member" if profile is not None else "invitation"

    async def revoke_invitation(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        invitation_id: str,
    ) -> None:
        validate_opaque_id(invitation_id, field="invitation_id")
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            invitation = (
                await session.execute(
                    select(self._tables.workspace_invitations).where(
                        self._tables.workspace_invitations.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_invitations.c.invitation_id == invitation_id,
                    )
                )
            ).one_or_none()
            if invitation is None:
                raise ManagementConflict("invitation does not exist")
            self._verify(
                invitation,
                "workspace_invitation",
                invitation_id=invitation.invitation_id,
                workspace_id=invitation.workspace_id,
                email=invitation.email,
                role=invitation.role,
                invited_by_principal_id=invitation.invited_by_principal_id,
            )
            if (
                WorkspaceRole(invitation.role) is WorkspaceRole.ADMIN
                and access.role is not WorkspaceRole.OWNER
            ):
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            await session.execute(
                delete(self._tables.workspace_invitations).where(
                    self._tables.workspace_invitations.c.invitation_id == invitation_id
                )
            )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action="workspace.invitation.revoke",
                target_kind="principal",
                target_id=invitation.email,
            )

    async def update_member_role(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        target_principal_id: str,
        role: WorkspaceRole,
    ) -> None:
        if role not in {WorkspaceRole.ADMIN, WorkspaceRole.MEMBER}:
            raise ValueError("workspace role is invalid")
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role is not WorkspaceRole.OWNER:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            target = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_members.c.principal_id
                        == target_principal_id,
                    )
                )
            ).one_or_none()
            if target is None:
                raise ManagementConflict("workspace member does not exist")
            self._verify(
                target,
                "workspace_member",
                workspace_id=target.workspace_id,
                principal_id=target.principal_id,
                role=target.role,
            )
            if WorkspaceRole(target.role) is WorkspaceRole.OWNER:
                raise ManagementConflict("transfer ownership before changing this role")
            values = self._signed(
                "workspace_member",
                workspace_id=access.workspace_id,
                principal_id=target_principal_id,
                role=role.value,
            )
            await session.execute(
                update(self._tables.workspace_members)
                .where(
                    self._tables.workspace_members.c.workspace_id == access.workspace_id,
                    self._tables.workspace_members.c.principal_id == target_principal_id,
                )
                .values(
                    role=role.value,
                    integrity_version=values["integrity_version"],
                    integrity_tag=values["integrity_tag"],
                )
            )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action="workspace.member.role.update",
                target_kind="principal",
                target_id=target_principal_id,
            )

    async def transfer_ownership(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        target_principal_id: str,
    ) -> None:
        if target_principal_id == principal_id:
            raise ManagementConflict("target is already the workspace owner")
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role is not WorkspaceRole.OWNER:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            target = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_members.c.principal_id
                        == target_principal_id,
                    )
                )
            ).one_or_none()
            if target is None:
                raise ManagementConflict("target must be a workspace member")
            self._verify(
                target,
                "workspace_member",
                workspace_id=target.workspace_id,
                principal_id=target.principal_id,
                role=target.role,
            )
            for member_id, role in (
                (principal_id, WorkspaceRole.ADMIN),
                (target_principal_id, WorkspaceRole.OWNER),
            ):
                values = self._signed(
                    "workspace_member",
                    workspace_id=access.workspace_id,
                    principal_id=member_id,
                    role=role.value,
                )
                await session.execute(
                    update(self._tables.workspace_members)
                    .where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_members.c.principal_id == member_id,
                    )
                    .values(
                        role=role.value,
                        integrity_version=values["integrity_version"],
                        integrity_tag=values["integrity_tag"],
                    )
                )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action="workspace.owner.transfer",
                target_kind="principal",
                target_id=target_principal_id,
            )

    async def remove_member(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        target_principal_id: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            target = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_members.c.principal_id
                        == target_principal_id,
                    )
                )
            ).one_or_none()
            if target is None:
                raise ManagementConflict("workspace member does not exist")
            self._verify(
                target,
                "workspace_member",
                workspace_id=target.workspace_id,
                principal_id=target.principal_id,
                role=target.role,
            )
            target_role = WorkspaceRole(target.role)
            if target_role is WorkspaceRole.OWNER:
                raise ManagementConflict("transfer ownership before removing the owner")
            if (
                target_role is WorkspaceRole.ADMIN
                and access.role is not WorkspaceRole.OWNER
            ):
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            await session.execute(
                delete(self._tables.agent_grants).where(
                    self._tables.agent_grants.c.workspace_id == access.workspace_id,
                    self._tables.agent_grants.c.principal_id == target_principal_id,
                )
            )
            await session.execute(
                delete(self._tables.agent_managers).where(
                    self._tables.agent_managers.c.workspace_id == access.workspace_id,
                    self._tables.agent_managers.c.principal_id == target_principal_id,
                )
            )
            await session.execute(
                delete(self._tables.workspace_members).where(
                    self._tables.workspace_members.c.workspace_id == access.workspace_id,
                    self._tables.workspace_members.c.principal_id == target_principal_id,
                )
            )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action="workspace.member.remove",
                target_kind="principal",
                target_id=target_principal_id,
            )

    async def list_agent_access(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        agent_slug: str,
    ) -> tuple[MemberAccess, ...]:
        members: list[MemberAccess] = []
        async with self._session_factory() as session:
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            agent = await self._agent_row(
                session,
                workspace_id=access.workspace_id,
                agent_slug=agent_slug,
            )
            await self._require_agent_manager(
                session,
                access=access,
                agent_profile_id=agent.agent_profile_id,
                principal_id=principal_id,
            )
            rows = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id
                    )
                )
            ).all()
            for row in rows:
                self._verify(
                    row,
                    "workspace_member",
                    workspace_id=row.workspace_id,
                    principal_id=row.principal_id,
                    role=row.role,
                )
                profile = (
                    await session.execute(
                        select(self._tables.principal_profiles).where(
                            self._tables.principal_profiles.c.principal_id
                            == row.principal_id
                        )
                    )
                ).one_or_none()
                if profile is not None:
                    self._verify(
                        profile,
                        "principal_profile",
                        principal_id=profile.principal_id,
                        email=profile.email,
                        display_name=profile.display_name,
                    )
                members.append(
                    MemberAccess(
                        principal_id=row.principal_id,
                        email=profile.email if profile is not None else None,
                        display_name=(
                            profile.display_name
                            if profile is not None
                            else None
                        ),
                        workspace_role=WorkspaceRole(row.role),
                        content_role=await self._content_grant(
                            session,
                            workspace_id=access.workspace_id,
                            agent_profile_id=agent.agent_profile_id,
                            principal_id=row.principal_id,
                        ),
                        explicit_manager=await self._is_explicit_manager(
                            session,
                            workspace_id=access.workspace_id,
                            agent_profile_id=agent.agent_profile_id,
                            principal_id=row.principal_id,
                        ),
                    )
                )
        return tuple(
            sorted(members, key=lambda item: item.email or item.principal_id)
        )

    async def set_content_access(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        agent_slug: str,
        target_principal_id: str,
        role: AgentGrantRole | None,
        allow_admin_self_grant: bool,
    ) -> None:
        if role is not None:
            role = AgentGrantRole(role)
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            agent = await self._agent_row(
                session,
                workspace_id=access.workspace_id,
                agent_slug=agent_slug,
            )
            await self._require_agent_manager(
                session,
                access=access,
                agent_profile_id=agent.agent_profile_id,
                principal_id=principal_id,
            )
            target = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_members.c.principal_id
                        == target_principal_id,
                    )
                )
            ).one_or_none()
            if target is None:
                raise ManagementConflict("target must be a workspace member")
            self._verify(
                target,
                "workspace_member",
                workspace_id=target.workspace_id,
                principal_id=target.principal_id,
                role=target.role,
            )
            existing = (
                await session.execute(
                    select(self._tables.agent_grants).where(
                        self._tables.agent_grants.c.workspace_id == access.workspace_id,
                        self._tables.agent_grants.c.agent_profile_id
                        == agent.agent_profile_id,
                        self._tables.agent_grants.c.principal_id == target_principal_id,
                    )
                )
            ).one_or_none()
            if existing is not None:
                self._verify(
                    existing,
                    "agent_grant",
                    workspace_id=existing.workspace_id,
                    agent_profile_id=existing.agent_profile_id,
                    principal_id=existing.principal_id,
                    role=existing.role,
                )
            if (
                role is not None
                and target_principal_id == principal_id
                and (
                    access.role not in _WORKSPACE_ADMIN_ROLES
                    or not allow_admin_self_grant
                )
            ):
                raise SelfGrantDisabled(
                    "administrator content self-grant is disabled by deployment policy"
                )
            if role is None:
                if existing is not None:
                    await session.execute(
                        delete(self._tables.agent_grants).where(
                            self._tables.agent_grants.c.agent_profile_id
                            == agent.agent_profile_id,
                            self._tables.agent_grants.c.principal_id
                            == target_principal_id,
                        )
                    )
                action = "agent.content.revoke"
            else:
                values = self._signed(
                    "agent_grant",
                    workspace_id=access.workspace_id,
                    agent_profile_id=agent.agent_profile_id,
                    principal_id=target_principal_id,
                    role=role.value,
                )
                await session.execute(
                    pg_insert(self._tables.agent_grants)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[
                            self._tables.agent_grants.c.agent_profile_id,
                            self._tables.agent_grants.c.principal_id,
                        ],
                        set_={
                            "role": role.value,
                            "integrity_version": values[
                                "integrity_version"
                            ],
                            "integrity_tag": values["integrity_tag"],
                        },
                    )
                )
                action = (
                    "agent.content.self_grant"
                    if target_principal_id == principal_id
                    else "agent.content.grant"
                )
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action=action,
                target_kind="principal",
                target_id=target_principal_id,
            )

    async def set_agent_manager(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        agent_slug: str,
        target_principal_id: str,
        enabled: bool,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            agent = await self._agent_row(
                session,
                workspace_id=access.workspace_id,
                agent_slug=agent_slug,
            )
            target = (
                await session.execute(
                    select(self._tables.workspace_members).where(
                        self._tables.workspace_members.c.workspace_id
                        == access.workspace_id,
                        self._tables.workspace_members.c.principal_id
                        == target_principal_id,
                    )
                )
            ).one_or_none()
            if target is None:
                raise ManagementConflict("target must be a workspace member")
            self._verify(
                target,
                "workspace_member",
                workspace_id=target.workspace_id,
                principal_id=target.principal_id,
                role=target.role,
            )
            existing = (
                await session.execute(
                    select(self._tables.agent_managers).where(
                        self._tables.agent_managers.c.agent_profile_id
                        == agent.agent_profile_id,
                        self._tables.agent_managers.c.principal_id == target_principal_id,
                    )
                )
            ).one_or_none()
            if existing is not None:
                self._verify(
                    existing,
                    "agent_manager",
                    workspace_id=existing.workspace_id,
                    agent_profile_id=existing.agent_profile_id,
                    principal_id=existing.principal_id,
                )
            if enabled:
                values = self._signed(
                    "agent_manager",
                    workspace_id=access.workspace_id,
                    agent_profile_id=agent.agent_profile_id,
                    principal_id=target_principal_id,
                )
                await session.execute(
                    pg_insert(self._tables.agent_managers)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[
                            self._tables.agent_managers.c.agent_profile_id,
                            self._tables.agent_managers.c.principal_id,
                        ],
                        set_={
                            "workspace_id": access.workspace_id,
                            "integrity_version": values[
                                "integrity_version"
                            ],
                            "integrity_tag": values["integrity_tag"],
                        },
                    )
                )
                action = "agent.manager.grant"
            else:
                if existing is not None:
                    await session.execute(
                        delete(self._tables.agent_managers).where(
                            self._tables.agent_managers.c.agent_profile_id
                            == agent.agent_profile_id,
                            self._tables.agent_managers.c.principal_id
                            == target_principal_id,
                        )
                    )
                action = "agent.manager.revoke"
            await self._audit(
                session,
                workspace_id=access.workspace_id,
                actor_principal_id=principal_id,
                action=action,
                target_kind="principal",
                target_id=target_principal_id,
            )

    async def list_audit_events(
        self,
        *,
        principal_id: str,
        workspace_slug: str,
        limit: int = 100,
    ) -> tuple[ManagementEvent, ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("audit limit is invalid")
        events: list[ManagementEvent] = []
        async with self._session_factory() as session:
            access = await self._workspace_access(
                session,
                workspace_slug=workspace_slug,
                principal_id=principal_id,
            )
            if access.role not in _WORKSPACE_ADMIN_ROLES:
                raise AuthorizationDenied(_AUTHORIZATION_DENIED)
            rows = (
                await session.execute(
                    select(self._tables.management_audit_events)
                    .where(
                        self._tables.management_audit_events.c.workspace_id
                        == access.workspace_id
                    )
                    .order_by(
                        self._tables.management_audit_events.c.occurred_at.desc(),
                        self._tables.management_audit_events.c.event_id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
            for row in rows:
                self._verify(
                    row,
                    "management_audit",
                    event_id=row.event_id,
                    workspace_id=row.workspace_id,
                    actor_principal_id=row.actor_principal_id,
                    action=row.action,
                    target_kind=row.target_kind,
                    target_id=row.target_id,
                )
                events.append(
                    ManagementEvent(
                        event_id=row.event_id,
                        actor_principal_id=row.actor_principal_id,
                        action=row.action,
                        target_kind=row.target_kind,
                        target_id=row.target_id,
                        occurred_at=row.occurred_at,
                    )
                )
        return tuple(events)


__all__ = [
    "AgentSummary",
    "InvitationSummary",
    "ManagementConflict",
    "ManagementEvent",
    "ManagementStore",
    "MemberAccess",
    "PrincipalProfile",
    "SelfGrantDisabled",
    "WorkspaceSummary",
    "content_role_from_name",
    "content_role_name",
    "normalize_email",
]
