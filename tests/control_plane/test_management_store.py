"""PostgreSQL acceptance tests for orthogonal management and content access."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
from uuid import uuid4

import pytest
from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import MemoryAction
from sqlalchemy import delete, text, update
from agent_filetree_memory.postgres import PostgresRuntime

from agent_filetree_memory.control_plane.management_store import (
    ManagementConflict,
    ManagementStore,
    SelfGrantConfirmationRequired,
    SelfGrantDisabled,
)
from agent_filetree_memory.control_plane.namespace_store import (
    AgentAccessPolicy,
    AgentGrantRole,
    NamespaceStore,
    WorkspaceAdmissionPolicy,
    WorkspaceAgentCreationPolicy,
    WorkspaceRole,
    agent_managers,
    namespace_metadata,
    namespace_tables_for_schema,
    workspace_policies,
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
                is_platform_admin=True,
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
            assert owner_agents[0].content_role is AgentGrantRole.ADMIN
            creator_binding = await namespaces.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=owner,
                action=MemoryAction.DELETE,
            )
            assert creator_binding.agent_role is AgentGrantRole.ADMIN
            await management.set_content_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=owner,
                role=None,
                allow_admin_self_grant=False,
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
            with pytest.raises(SelfGrantConfirmationRequired):
                await management.set_content_access(
                    principal_id=owner,
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    target_principal_id=owner,
                    role=AgentGrantRole.READER,
                    allow_admin_self_grant=True,
                )
            await management.set_content_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=owner,
                role=AgentGrantRole.READER,
                allow_admin_self_grant=True,
                self_grant_confirmed=True,
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

            events = await management.list_audit_events(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            assert any(
                event.action == "agent.content.self_grant"
                and event.target_id == owner
                for event in events
            )
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_workspace_read_policy_is_explicit_bounded_and_audited() -> None:
    suffix = uuid4().hex
    workspace_slug = f"shared-{suffix}"
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
                is_platform_admin=True,
            )
            created = await management.create_agent(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                display_alias="Shared agent",
            )
            assert created.access_policy is AgentAccessPolicy.PRIVATE
            assert created.explicit_content_role is AgentGrantRole.ADMIN
            await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"member-{suffix}@example.test",
                role=WorkspaceRole.MEMBER,
            )
            assert await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            ) == ()

            shared = await management.set_agent_access_policy(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                access_policy=AgentAccessPolicy.WORKSPACE_READ,
                allow_admin_self_grant=False,
            )
            assert shared.access_policy is AgentAccessPolicy.WORKSPACE_READ
            member_agents = await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            )
            assert len(member_agents) == 1
            assert member_agents[0].content_role is AgentGrantRole.READER
            assert member_agents[0].explicit_content_role is None
            assert member_agents[0].can_manage is False

            inherited = await namespaces.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=member,
                action=MemoryAction.READ,
            )
            assert inherited.agent_role is AgentGrantRole.READER
            with pytest.raises(AuthorizationDenied):
                await namespaces.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=member,
                    action=MemoryAction.WRITE,
                )

            access = await management.list_agent_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
            )
            by_principal = {item.principal_id: item for item in access}
            assert by_principal[member].content_role is None
            assert (
                by_principal[member].effective_content_role
                is AgentGrantRole.READER
            )

            await management.set_content_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=member,
                role=AgentGrantRole.EDITOR,
                allow_admin_self_grant=False,
            )
            explicit = await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            )
            assert explicit[0].content_role is AgentGrantRole.EDITOR
            assert explicit[0].explicit_content_role is AgentGrantRole.EDITOR

            await management.set_content_access(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                target_principal_id=member,
                role=None,
                allow_admin_self_grant=False,
            )
            fallback = await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            )
            assert fallback[0].content_role is AgentGrantRole.READER
            assert fallback[0].explicit_content_role is None

            private = await management.set_agent_access_policy(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                access_policy=AgentAccessPolicy.PRIVATE,
                allow_admin_self_grant=False,
            )
            assert private.access_policy is AgentAccessPolicy.PRIVATE
            assert await management.list_agents(
                principal_id=member,
                workspace_slug=workspace_slug,
            ) == ()

            events = await management.list_audit_events(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            actions = {event.action for event in events}
            assert "agent.workspace_read.enable" in actions
            assert "agent.workspace_read.disable" in actions
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_workspace_read_cannot_bypass_self_grant_policy() -> None:
    suffix = uuid4().hex
    workspace_slug = f"shared-self-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    administrator = f"oidc:tenant:admin-{suffix}"

    async with _live_stores() as (management, _namespaces, sessions):
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
                is_platform_admin=True,
            )
            await management.create_agent(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                display_alias="Shared agent",
            )
            await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"admin-{suffix}@example.test",
                role=WorkspaceRole.ADMIN,
            )

            with pytest.raises(SelfGrantDisabled):
                await management.set_agent_access_policy(
                    principal_id=administrator,
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    access_policy=AgentAccessPolicy.WORKSPACE_READ,
                    allow_admin_self_grant=False,
                )
            with pytest.raises(SelfGrantConfirmationRequired):
                await management.set_agent_access_policy(
                    principal_id=administrator,
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    access_policy=AgentAccessPolicy.WORKSPACE_READ,
                    allow_admin_self_grant=True,
                )
            shared = await management.set_agent_access_policy(
                principal_id=administrator,
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                access_policy=AgentAccessPolicy.WORKSPACE_READ,
                allow_admin_self_grant=True,
                self_grant_confirmed=True,
            )
            assert shared.content_role is AgentGrantRole.READER
            assert shared.explicit_content_role is None
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
                is_platform_admin=True,
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
                is_platform_admin=True,
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
                self_grant_confirmed=True,
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
                is_platform_admin=True,
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


@pytest.mark.live
async def test_platform_admin_visibility_and_all_member_creation_policy() -> None:
    suffix = uuid4().hex
    workspace_slug = f"open-{suffix}"
    isolated_slug = f"isolated-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    platform_observer = f"oidc:tenant:platform-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_stores() as (management, namespaces, sessions):
        try:
            for principal, local_part in (
                (owner, "owner"),
                (platform_observer, "platform"),
                (member, "member"),
            ):
                await _register(
                    management,
                    principal,
                    f"{local_part}-{suffix}@example.test",
                )

            with pytest.raises(AuthorizationDenied):
                await management.create_workspace(
                    principal_id=member,
                    workspace_slug=workspace_slug,
                    is_platform_admin=False,
                )

            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
                admission_policy=(
                    WorkspaceAdmissionPolicy.ALL_AUTHENTICATED
                ),
                agent_creation_policy=(
                    WorkspaceAgentCreationPolicy.ALL_MEMBERS
                ),
                is_platform_admin=True,
            )
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=isolated_slug,
                is_platform_admin=True,
            )
            await management.create_agent(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=f"owner-agent-{suffix}",
                display_alias="Owner agent",
            )

            global_inventory = await management.list_workspaces(
                principal_id=platform_observer,
                is_platform_admin=True,
            )
            by_slug = {item.slug: item for item in global_inventory}
            assert by_slug[workspace_slug].role is None
            assert by_slug[workspace_slug].agent_count == 1
            with pytest.raises(AuthorizationDenied):
                await management.list_agents(
                    principal_id=platform_observer,
                    workspace_slug=workspace_slug,
                )

            assigned = await management.assign_platform_admin_role(
                principal_id=platform_observer,
                workspace_slug=workspace_slug,
                is_platform_admin=True,
            )
            assert assigned.role is WorkspaceRole.ADMIN
            visible = await management.list_agents(
                principal_id=platform_observer,
                workspace_slug=workspace_slug,
            )
            assert [item.slug for item in visible] == [f"owner-agent-{suffix}"]
            assert visible[0].content_role is None
            with pytest.raises(AuthorizationDenied):
                await namespaces.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=visible[0].slug,
                    principal_id=platform_observer,
                    action=MemoryAction.READ,
                )

            assert await management.ensure_workspace_admission(
                principal_id=member,
                email=f"member-{suffix}@example.test",
                display_name="Member",
                workspace_slug=workspace_slug,
            ) is WorkspaceRole.MEMBER
            member_agent = await management.create_agent(
                principal_id=member,
                workspace_slug=workspace_slug,
                agent_slug=f"member-agent-{suffix}",
                display_alias="Member agent",
            )
            assert member_agent.can_manage is True
            assert member_agent.content_role is AgentGrantRole.ADMIN
            access = await management.list_agent_access(
                principal_id=member,
                workspace_slug=workspace_slug,
                agent_slug=member_agent.slug,
            )
            member_access = next(
                item for item in access if item.principal_id == member
            )
            assert member_access.explicit_manager is True
            assert member_access.content_role is AgentGrantRole.ADMIN

            with pytest.raises(AuthorizationDenied):
                await management.list_agents(
                    principal_id=member,
                    workspace_slug=isolated_slug,
                )
        finally:
            await _delete_workspace(sessions, workspace_slug)
            await _delete_workspace(sessions, isolated_slug)


@pytest.mark.live
async def test_provider_neutral_workspace_admission_policies_fail_closed() -> None:
    suffix = uuid4().hex
    external_slug = f"external-{suffix}"
    authenticated_slug = f"authenticated-{suffix}"
    invite_only_slug = f"private-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    entitled = f"oidc:tenant:entitled-{suffix}"
    denied = f"oidc:tenant:denied-{suffix}"
    requests = []

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            for principal, local_part in (
                (owner, "owner"),
                (entitled, "entitled"),
                (denied, "denied"),
            ):
                await _register(
                    management,
                    principal,
                    f"{local_part}-{suffix}@example.test",
                )
            for slug, admission in (
                (external_slug, WorkspaceAdmissionPolicy.EXTERNAL_ENTITLEMENT),
                (authenticated_slug, WorkspaceAdmissionPolicy.ALL_AUTHENTICATED),
                (invite_only_slug, WorkspaceAdmissionPolicy.INVITE_ONLY),
            ):
                await management.create_workspace(
                    principal_id=owner,
                    workspace_slug=slug,
                    admission_policy=admission,
                    is_platform_admin=True,
                )

            async def entitlement_resolver(request):
                requests.append(request)
                return request.principal_id == entitled

            entitled_store = ManagementStore(
                sessions,
                integrity_key=_INTEGRITY_KEY,
                tables=namespace_tables_for_schema(),
                entitlement_resolver=entitlement_resolver,
            )
            assert await entitled_store.ensure_workspace_admission(
                principal_id=entitled,
                email=f"entitled-{suffix}@example.test",
                display_name="Entitled person",
                workspace_slug=external_slug,
            ) is WorkspaceRole.MEMBER
            assert requests[-1].workspace_slug == external_slug
            assert requests[-1].email == f"entitled-{suffix}@example.test"

            with pytest.raises(AuthorizationDenied):
                await entitled_store.ensure_workspace_admission(
                    principal_id=denied,
                    email=f"denied-{suffix}@example.test",
                    display_name="Denied person",
                    workspace_slug=external_slug,
                )
            with pytest.raises(AuthorizationDenied):
                await management.ensure_workspace_admission(
                    principal_id=denied,
                    email=f"denied-{suffix}@example.test",
                    display_name="Denied person",
                    workspace_slug=external_slug,
                )
            with pytest.raises(AuthorizationDenied):
                await management.ensure_workspace_admission(
                    principal_id=denied,
                    email=f"denied-{suffix}@example.test",
                    display_name="Denied person",
                    workspace_slug=invite_only_slug,
                )
            assert await management.ensure_workspace_admission(
                principal_id=denied,
                email=f"denied-{suffix}@example.test",
                display_name="Denied person",
                workspace_slug=authenticated_slug,
            ) is WorkspaceRole.MEMBER

            async def broken_resolver(_request):
                raise RuntimeError("provider unavailable")

            broken_store = ManagementStore(
                sessions,
                integrity_key=_INTEGRITY_KEY,
                tables=namespace_tables_for_schema(),
                entitlement_resolver=broken_resolver,
            )
            newcomer = f"oidc:tenant:new-{suffix}"
            await _register(
                management,
                newcomer,
                f"new-{suffix}@example.test",
            )
            with pytest.raises(AuthorizationDenied):
                await broken_store.ensure_workspace_admission(
                    principal_id=newcomer,
                    email=f"new-{suffix}@example.test",
                    display_name="New person",
                    workspace_slug=external_slug,
                )

            invalid_store = ManagementStore(
                sessions,
                integrity_key=_INTEGRITY_KEY,
                tables=namespace_tables_for_schema(),
                entitlement_resolver=lambda _request: 1,
            )
            invalid_principal = f"oidc:tenant:invalid-{suffix}"
            await _register(
                management,
                invalid_principal,
                f"invalid-{suffix}@example.test",
            )
            with pytest.raises(AuthorizationDenied):
                await invalid_store.ensure_workspace_admission(
                    principal_id=invalid_principal,
                    email=f"invalid-{suffix}@example.test",
                    display_name="Invalid result",
                    workspace_slug=external_slug,
                )
        finally:
            await _delete_workspace(sessions, external_slug)
            await _delete_workspace(sessions, authenticated_slug)
            await _delete_workspace(sessions, invite_only_slug)


@pytest.mark.live
async def test_manager_without_content_can_manage_and_transfer_authority() -> None:
    suffix = uuid4().hex
    workspace_slug = f"transfer-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    first = f"oidc:tenant:first-{suffix}"
    second = f"oidc:tenant:second-{suffix}"

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            for principal, local_part in (
                (owner, "owner"),
                (first, "first"),
                (second, "second"),
            ):
                await _register(
                    management,
                    principal,
                    f"{local_part}-{suffix}@example.test",
                )
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
                admission_policy=WorkspaceAdmissionPolicy.ALL_AUTHENTICATED,
                agent_creation_policy=WorkspaceAgentCreationPolicy.ALL_MEMBERS,
                is_platform_admin=True,
            )
            for principal, local_part in ((first, "first"), (second, "second")):
                await management.ensure_workspace_admission(
                    principal_id=principal,
                    email=f"{local_part}-{suffix}@example.test",
                    display_name=local_part.title(),
                    workspace_slug=workspace_slug,
                )
            agent = await management.create_agent(
                principal_id=first,
                workspace_slug=workspace_slug,
                agent_slug=f"agent-{suffix}",
                display_alias="Transferred agent",
            )
            await management.set_content_access(
                principal_id=first,
                workspace_slug=workspace_slug,
                agent_slug=agent.slug,
                target_principal_id=first,
                role=None,
                allow_admin_self_grant=False,
            )
            first_view = await management.list_agents(
                principal_id=first,
                workspace_slug=workspace_slug,
            )
            assert first_view[0].can_manage is True
            assert first_view[0].content_role is None

            await management.transfer_agent_management(
                principal_id=first,
                workspace_slug=workspace_slug,
                agent_slug=agent.slug,
                target_principal_id=second,
            )
            assert await management.list_agents(
                principal_id=first,
                workspace_slug=workspace_slug,
            ) == ()
            second_view = await management.list_agents(
                principal_id=second,
                workspace_slug=workspace_slug,
            )
            assert second_view[0].can_manage is True
            assert second_view[0].content_role is None

            await management.set_content_access(
                principal_id=second,
                workspace_slug=workspace_slug,
                agent_slug=agent.slug,
                target_principal_id=first,
                role=AgentGrantRole.READER,
                allow_admin_self_grant=False,
            )
            second_view = await management.list_agents(
                principal_id=second,
                workspace_slug=workspace_slug,
            )
            assert second_view[0].content_role is None
            first_view = await management.list_agents(
                principal_id=first,
                workspace_slug=workspace_slug,
            )
            assert first_view[0].content_role is AgentGrantRole.READER
            assert first_view[0].can_manage is False
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_workspace_and_agent_transfers_do_not_move_content_grants() -> None:
    suffix = uuid4().hex
    workspace_slug = f"ownership-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    successor = f"oidc:tenant:successor-{suffix}"

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await _register(
                management,
                successor,
                f"successor-{suffix}@example.test",
            )
            await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
                is_platform_admin=True,
            )
            agent = await management.create_agent(
                principal_id=owner,
                workspace_slug=workspace_slug,
                agent_slug=f"agent-{suffix}",
                display_alias="Ownership agent",
            )
            await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"successor-{suffix}@example.test",
                role=WorkspaceRole.MEMBER,
            )
            await management.transfer_ownership(
                principal_id=owner,
                workspace_slug=workspace_slug,
                target_principal_id=successor,
            )
            successor_agents = await management.list_agents(
                principal_id=successor,
                workspace_slug=workspace_slug,
            )
            assert successor_agents[0].can_manage is True
            assert successor_agents[0].content_role is None
            old_owner_agents = await management.list_agents(
                principal_id=owner,
                workspace_slug=workspace_slug,
            )
            assert old_owner_agents[0].content_role is AgentGrantRole.ADMIN
            assert old_owner_agents[0].slug == agent.slug
            await management.remove_member(
                principal_id=successor,
                workspace_slug=workspace_slug,
                target_principal_id=owner,
            )
            with pytest.raises(AuthorizationDenied):
                await management.list_agents(
                    principal_id=owner,
                    workspace_slug=workspace_slug,
                )
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_concurrent_platform_workspace_creation_has_one_owner() -> None:
    suffix = uuid4().hex
    workspace_slug = f"workspace-race-{suffix}"
    principals = (
        f"oidc:tenant:first-{suffix}",
        f"oidc:tenant:second-{suffix}",
    )

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            for index, principal in enumerate(principals):
                await _register(
                    management,
                    principal,
                    f"platform-{index}-{suffix}@example.test",
                )
            results = await asyncio.gather(
                *(
                    management.create_workspace(
                        principal_id=principal,
                        workspace_slug=workspace_slug,
                        is_platform_admin=True,
                    )
                    for principal in principals
                ),
                return_exceptions=True,
            )
            successes = [
                item for item in results if not isinstance(item, BaseException)
            ]
            failures = [
                item for item in results if isinstance(item, BaseException)
            ]
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], ManagementConflict)
            inventory = await management.list_workspaces(
                principal_id=principals[0],
                is_platform_admin=True,
            )
            assert len(
                [item for item in inventory if item.slug == workspace_slug]
            ) == 1
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_missing_policy_row_falls_back_to_least_privilege() -> None:
    suffix = uuid4().hex
    workspace_slug = f"legacy-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_stores() as (management, _namespaces, sessions):
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await _register(management, member, f"member-{suffix}@example.test")
            workspace = await management.create_workspace(
                principal_id=owner,
                workspace_slug=workspace_slug,
                admission_policy=WorkspaceAdmissionPolicy.ALL_AUTHENTICATED,
                agent_creation_policy=WorkspaceAgentCreationPolicy.ALL_MEMBERS,
                is_platform_admin=True,
            )
            await management.invite_member(
                principal_id=owner,
                workspace_slug=workspace_slug,
                email=f"member-{suffix}@example.test",
                role=WorkspaceRole.MEMBER,
            )
            async with sessions() as session, session.begin():
                await session.execute(
                    delete(workspace_policies).where(
                        workspace_policies.c.workspace_id
                        == workspace.workspace_id
                    )
                )

            summary = next(
                item
                for item in await management.list_workspaces(
                    principal_id=member
                )
                if item.slug == workspace_slug
            )
            assert summary.admission_policy is WorkspaceAdmissionPolicy.INVITE_ONLY
            assert (
                summary.agent_creation_policy
                is WorkspaceAgentCreationPolicy.ADMINS_ONLY
            )
            assert summary.can_create_agents is False
            with pytest.raises(AuthorizationDenied):
                await management.create_agent(
                    principal_id=member,
                    workspace_slug=workspace_slug,
                    agent_slug=f"agent-{suffix}",
                    display_alias="Denied agent",
                )
        finally:
            await _delete_workspace(sessions, workspace_slug)


@pytest.mark.live
async def test_self_service_creation_invited_membership_and_atomic_quota() -> None:
    suffix = uuid4().hex
    owner, member = f"owner-{suffix}", f"member-{suffix}"
    shared, own, raced = f"shared-{suffix}", f"own-{suffix}", f"raced-{suffix}"
    async with _live_stores() as (_management, namespaces, sessions):
        management = ManagementStore(
            sessions,
            integrity_key=_INTEGRITY_KEY,
            tables=namespace_tables_for_schema(),
            max_workspaces_per_principal=1,
        )
        try:
            await _register(management, owner, f"owner-{suffix}@example.test")
            await _register(management, member, f"member-{suffix}@example.test")
            with pytest.raises(AuthorizationDenied):
                await management.create_workspace(
                    principal_id=owner, workspace_slug=shared
                )
            workspace = await management.create_workspace(
                principal_id=owner, workspace_slug=shared, can_create_workspaces=True
            )
            assert workspace.role is WorkspaceRole.OWNER
            assert workspace.admission_policy is WorkspaceAdmissionPolicy.INVITE_ONLY
            assert workspace.can_create_agents is True
            await management.create_agent(
                principal_id=owner,
                workspace_slug=shared,
                agent_slug="assistant",
                display_alias="Assistant",
            )
            assert (
                await management.invite_member(
                    principal_id=owner,
                    workspace_slug=shared,
                    email=f"member-{suffix}@example.test",
                    role=WorkspaceRole.MEMBER,
                )
                == "member"
            )
            assert await management.workspace_creation_usage(principal_id=member) == (
                0,
                1,
            )
            # Joining a workspace doesn't spend the one creation slot. Parallel requests do.
            results = await asyncio.gather(
                *[
                    management.create_workspace(
                        principal_id=member,
                        workspace_slug=slug,
                        can_create_workspaces=True,
                    )
                    for slug in (own, raced)
                ],
                return_exceptions=True,
            )
            successes = [r for r in results if not isinstance(r, BaseException)]
            assert len(successes) == 1
            assert sum(isinstance(r, ManagementConflict) for r in results) == 1
            assert successes[0].role is WorkspaceRole.OWNER
            assert await management.workspace_creation_usage(principal_id=member) == (
                1,
                1,
            )
            assert len(await management.list_workspaces(principal_id=member)) == 2
            # The new owner gains neither administration nor content access in the invited workspace.
            with pytest.raises(AuthorizationDenied):
                await management.create_agent(
                    principal_id=member,
                    workspace_slug=shared,
                    agent_slug="not-allowed",
                    display_alias="Not allowed",
                )
            with pytest.raises(AuthorizationDenied):
                await management.assign_platform_admin_role(
                    principal_id=member, workspace_slug=shared, is_platform_admin=False
                )
            with pytest.raises(AuthorizationDenied):
                await namespaces.resolve_or_create(
                    principal_id=member,
                    workspace_slug=shared,
                    agent_slug="assistant",
                    action=MemoryAction.READ,
                )
        finally:
            for slug in (shared, own, raced):
                await _delete_workspace(sessions, slug)

