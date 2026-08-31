"""PostgreSQL acceptance tests for orthogonal management and content access."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from uuid import uuid4

import pytest
from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import MemoryAction
from sqlalchemy import delete, text, update
from agent_filetree_memory.postgres import PostgresRuntime

from agent_filetree_memory.control_plane.management_store import (
    ManagementStore,
    SelfGrantDisabled,
)
from agent_filetree_memory.control_plane.namespace_store import (
    AgentGrantRole,
    NamespaceStore,
    WorkspaceRole,
    agent_managers,
    namespace_metadata,
    namespace_tables_for_schema,
    workspaces,
)

_INTEGRITY_KEY = b"management-integrity-test-key-0123456789abcdef"


def _postgres_url() -> str:
    value = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "set AGENT_FILETREE_MEMORY_TEST_DATABASE_URL to a disposable "
            "PostgreSQL database"
        )
    return value


@asynccontextmanager
async def _live_stores():
    runtime = PostgresRuntime.from_url(_postgres_url())
    assert runtime.engine is not None
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("CREATE SCHEMA IF NOT EXISTS agent_filetree_memory")
            )
            await connection.run_sync(namespace_metadata.create_all)
        tables = namespace_tables_for_schema()
        yield (
            ManagementStore(
                runtime.session_factory,
                integrity_key=_INTEGRITY_KEY,
                tables=tables,
            ),
            NamespaceStore(
                runtime.session_factory,
                integrity_key=_INTEGRITY_KEY,
                tables=tables,
            ),
            runtime.session_factory,
        )
    finally:
        await runtime.close()


async def _delete_workspace(session_factory, workspace_slug: str) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            delete(workspaces).where(workspaces.c.slug == workspace_slug)
        )


async def _register(store: ManagementStore, principal: str, email: str) -> None:
    await store.register_principal(
        principal_id=principal,
        email=email,
        display_name=email.split("@", 1)[0].title(),
    )


@pytest.mark.live
async def test_management_and_content_access_are_orthogonal() -> None:
    suffix = uuid4().hex
    workspace_slug = f"managed-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_stores() as (management, namespaces, sessions):
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await _register(management, member, f"member-{suffix}@example.test")
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            await management.create_agent(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                display_alias="Managed agent",
            )

            owner_agents = await management.list_agents(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            assert len(owner_agents) == 1
            assert owner_agents[0].can_manage is True
            assert owner_agents[0].content_role is None
            with pytest.raises(AuthorizationDenied):
                await namespaces.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=owner,
                    action=MemoryAction.READ,
                )

            assert await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"member-{suffix}@example.test",
                role=WorkspaceRole.MEMBER,
            ) == "member"
            assert await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            ) == ()

            await management.set_agent_manager(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=member,
                enabled=True,
            )
            member_agents = await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            )
            assert member_agents[0].can_manage is True
            assert member_agents[0].content_role is None
            with pytest.raises(SelfGrantDisabled):
                await management.set_content_access(
                    principal_id=member,
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    target_principal_id=member,
                    role=AgentGrantRole.READER,
                    allow_admin_self_grant=True,
                )

            with pytest.raises(SelfGrantDisabled):
                await management.set_content_access(
                    principal_id=owner,
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    target_principal_id=owner,
                    role=AgentGrantRole.READER,
                    allow_admin_self_grant=False,
                )
            await management.set_content_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=owner,
                role=AgentGrantRole.READER,
                allow_admin_self_grant=True,
            )
            owner_binding = await namespaces.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=owner,
                action=MemoryAction.READ,
            )
            assert owner_binding.agent_role is AgentGrantRole.READER

            await management.set_content_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=member,
                role=AgentGrantRole.EDITOR,
                allow_admin_self_grant=True,
            )
            await management.set_agent_manager(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=member,
                enabled=False,
            )
            member_agents = await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            )
            assert member_agents[0].can_manage is False
            assert member_agents[0].content_role is AgentGrantRole.EDITOR

            access = await management.list_agent_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
            )
            by_principal = {item.principal_id: item for item in access}
            assert by_principal[owner].workspace_role is WorkspaceRole.OWNER
            assert by_principal[owner].content_role is AgentGrantRole.READER
            assert by_principal[member].explicit_manager is False
            assert by_principal[member].content_role is AgentGrantRole.EDITOR
        finally:
            await _delete_workspace(sessions, workspace_slug)

@pytest.mark.live
async def test_pending_invitation_binds_only_after_verified_sign_in() -> None:
    suffix = uuid4().hex
    workspace_slug = f"invited-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    invitee = f"oidc:tenant:invitee-{suffix}"
    invitee_email = f"invitee-{suffix}@example.test"

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            assert await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=invitee_email,
                role=WorkspaceRole.ADMIN,
            ) == "invitation"
            _members, invitations = await management.list_members(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            assert [item.email for item in invitations] == [invitee_email]

            await _register(management, invitee, invitee_email)
            workspaces_for_invitee = await management.list_workspaces(
                principal_id=invitee
            )
            assert workspaces_for_invitee[0].role is WorkspaceRole.ADMIN
            _members, invitations = await management.list_members(
                principal_id=invitee,
                workspace_slug=workspace_slug,
            )
            assert invitations == ()
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_workspace_admin_lists_agents_without_content_access() -> None:
    suffix = uuid4().hex
    workspace_slug = f"admin-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    administrator = f"oidc:tenant:admin-{suffix}"

    async with _live_stores() as (management, namespaces, sessions):
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await _register(
                management,
                administrator,
                f"admin-{suffix}@example.test",
            )
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            for index in range(2):
                await management.create_agent(
                    principal_id=owner,
                    workspace_slug=workspace_slug,
                    agent_slug=f"agent-{index}-{suffix}",
                    display_alias=f"Agent {index}",
                )
            await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"admin-{suffix}@example.test",
                role=WorkspaceRole.ADMIN,
            )

            agents = await management.list_agents(
                principal_id=administrator,
                workspace_slug=workspace_slug,
            )
            assert len(agents) == 2
            assert all(item.can_manage for item in agents)
            assert all(item.content_role is None for item in agents)
            with pytest.raises(AuthorizationDenied):
                await namespaces.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agents[0].slug,
                    principal_id=administrator,
                    action=MemoryAction.READ,
                )
            await management.set_content_access(
                principal_id=administrator,
                workspace_slug=workspace_slug,
                agent_slug=agents[0].slug,
                target_principal_id=administrator,
                role=AgentGrantRole.EDITOR,
                allow_admin_self_grant=True,
            )
            binding = await namespaces.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agents[0].slug,
                principal_id=administrator,
                action=MemoryAction.WRITE,
            )
            assert binding.agent_role is AgentGrantRole.EDITOR
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_forged_agent_manager_row_fails_closed() -> None:
    suffix = uuid4().hex
    workspace_slug = f"manager-integrity-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await _register(management, member, f"member-{suffix}@example.test")
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            agent = await management.create_agent(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                display_alias="Integrity agent",
            )
            await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"member-{suffix}@example.test",
                role=WorkspaceRole.MEMBER,
            )
            await management.set_agent_manager(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=member,
                enabled=True,
            )
            async with sessions() as session, session.begin():
                await session.execute(
                    update(agent_managers)
                    .where(
                        agent_managers.c.agent_profile_id
                        == agent.agent_profile_id,
                        agent_managers.c.principal_id == member,
                    )
                    .values(integrity_tag=b"\x00" * 32)
                )

            with pytest.raises(AuthorizationDenied):
                await management.list_agents(
                    principal_id=member,
                    workspace_slug=workspace_slug,
                )
        finally:
            await _delete_workspace(sessions, workspace_slug)
