"""Focused namespace-store authorization and PostgreSQL concurrency tests."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import os

import pytest
from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import MemoryAction
from sqlalchemy import delete, select, text, update
from agent_filetree_memory.postgres import PostgresRuntime

from agent_filetree_memory.control_plane.namespace_store import (
    AgentGrantRole,
    NamespaceStore,
    WorkspaceAdmissionPolicy,
    WorkspaceAgentCreationPolicy,
    WorkspaceRole,
    _record_integrity_tag,
    agent_grants,
    agent_managers,
    agent_profiles,
    namespace_metadata,
    role_allows_action,
    validate_slug,
    workspace_members,
    workspace_policies,
    workspaces,
)

_INTEGRITY_KEY = b"namespace-integrity-test-key-0123456789abcdef"


def _signed_values(record_type: str, **fields: str) -> dict[str, object]:
    return {
        **fields,
        "integrity_version": 1,
        "integrity_tag": _record_integrity_tag(
            _INTEGRITY_KEY,
            record_type,
            **fields,
        ),
    }


@pytest.mark.parametrize(
    "value",
    ["agent", "agent-1", "a", "a" * 63, "123-agent"],
)
def test_validate_slug_accepts_conservative_lowercase_aliases(value: str) -> None:
    assert validate_slug(value, field="slug") == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Agent",
        "agent_name",
        "-agent",
        "agent-",
        "agent/name",
        "agent name",
        "a" * 64,
        "agent--profile\x00",
    ],
)
def test_validate_slug_rejects_ambiguous_or_unsafe_aliases(value: str) -> None:
    with pytest.raises(ValueError, match="lowercase slug"):
        validate_slug(value, field="slug")


def test_namespace_metadata_is_schema_bound_and_separates_identity_metadata() -> None:
    expected = {
        "workspaces",
        "workspace_members",
        "workspace_policies",
        "agent_profiles",
        "agent_grants",
        "agent_managers",
        "management_audit_events",
        "principal_profiles",
        "workspace_invitations",
    }
    assert {table.name for table in namespace_metadata.tables.values()} == expected
    assert all(
        table.schema == "agent_filetree_memory"
        for table in namespace_metadata.tables.values()
    )
    tables_with_email = {
        table.name
        for table in namespace_metadata.tables.values()
        if "email" in table.c
    }
    assert tables_with_email == {
        "principal_profiles",
        "workspace_invitations",
    }
    assert all(
        "content" not in column.name and "path" not in column.name
        for table in namespace_metadata.tables.values()
        for column in table.c
    )
    assert all(
        "integrity_version" in table.c and "integrity_tag" in table.c
        for table in namespace_metadata.tables.values()
    )

    assert any(
        constraint.name == "uq_afm_workspaces_slug"
        for constraint in workspaces.constraints
    )


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (AgentGrantRole.READER, {MemoryAction.LIST, MemoryAction.READ}),
        (
            AgentGrantRole.EDITOR,
            {
                MemoryAction.LIST,
                MemoryAction.READ,
                MemoryAction.WRITE,
                MemoryAction.APPEND,
            },
        ),
        (
            AgentGrantRole.ADMIN,
            {
                MemoryAction.LIST,
                MemoryAction.READ,
                MemoryAction.WRITE,
                MemoryAction.APPEND,
                MemoryAction.DELETE,
            },
        ),
    ],
)
def test_agent_roles_authorize_only_the_bounded_actions(
    role: AgentGrantRole,
    allowed: set[MemoryAction],
) -> None:
    for action in MemoryAction:
        assert role_allows_action(role, action) is (action in allowed)


async def test_invalid_slug_is_rejected_before_session_creation() -> None:
    def fail_factory():
        raise AssertionError("invalid aliases must not open a database session")

    store = NamespaceStore(fail_factory, integrity_key=_INTEGRITY_KEY)
    with pytest.raises(ValueError, match="lowercase slug"):
        await store.resolve_or_create(
            workspace_slug="Not-Lowercase",
            agent_slug="agent",
            principal_id="oidc:tenant:object",
            action=MemoryAction.READ,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_workspaces_per_principal", 0),
        ("max_workspaces_per_principal", True),
        ("max_agents_per_workspace", -1),
        ("max_agents_per_workspace", "10"),
    ],
)
def test_namespace_limits_require_positive_integers(
    field: str,
    value: object,
) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError, match=f"{field} must be a positive integer"):
        NamespaceStore(
            lambda: None,
            integrity_key=_INTEGRITY_KEY,
            **kwargs,
        )


def _postgres_url() -> str:
    value = os.environ.get("AGENT_FILETREE_MEMORY_TEST_DATABASE_URL")
    if not value:
        pytest.skip(
            "set AGENT_FILETREE_MEMORY_TEST_DATABASE_URL to a disposable "
            "PostgreSQL database"
        )
    return value


@asynccontextmanager
async def _live_store():
    runtime = PostgresRuntime.from_url(_postgres_url())
    assert runtime.engine is not None
    try:
        async with runtime.engine.begin() as connection:
            await connection.execute(
                text("CREATE SCHEMA IF NOT EXISTS agent_filetree_memory")
            )
            await connection.run_sync(namespace_metadata.create_all)
        yield (
            NamespaceStore(
                runtime.session_factory,
                integrity_key=_INTEGRITY_KEY,
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


async def _seed_workspace(
    session_factory,
    *,
    workspace_slug: str,
    principal_id: str,
    role: WorkspaceRole = WorkspaceRole.OWNER,
    agent_creation_policy: WorkspaceAgentCreationPolicy = (
        WorkspaceAgentCreationPolicy.ADMINS_ONLY
    ),
) -> str:
    """Create the control-plane state that the MCP path may never create."""

    workspace_id = uuid4().hex
    async with session_factory() as session, session.begin():
        await session.execute(
            workspaces.insert().values(
                **_signed_values(
                    "workspace",
                    workspace_id=workspace_id,
                    slug=workspace_slug,
                    created_by_principal_id=principal_id,
                )
            )
        )
        await session.execute(
            workspace_members.insert().values(
                **_signed_values(
                    "workspace_member",
                    workspace_id=workspace_id,
                    principal_id=principal_id,
                    role=role.value,
                )
            )
        )
        await session.execute(
            workspace_policies.insert().values(
                **_signed_values(
                    "workspace_policy",
                    workspace_id=workspace_id,
                    admission_policy=WorkspaceAdmissionPolicy.INVITE_ONLY.value,
                    agent_creation_policy=agent_creation_policy.value,
                )
            )
        )
    return workspace_id


@pytest.mark.live
async def test_live_membership_and_agent_grants_fail_closed() -> None:
    suffix = uuid4().hex
    workspace_slug = f"namespace-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=owner,
            )
            owner_binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=owner,
                action=MemoryAction.DELETE,
                display_alias="Namespace integration agent",
            )
            assert owner_binding.workspace_role is WorkspaceRole.OWNER
            assert owner_binding.agent_role is AgentGrantRole.ADMIN

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=member,
                    action=MemoryAction.READ,
                )

            async with session_factory() as session, session.begin():
                await session.execute(
                    workspace_members.insert().values(
                        **_signed_values(
                            "workspace_member",
                            workspace_id=owner_binding.workspace_id,
                            principal_id=member,
                            role=WorkspaceRole.MEMBER.value,
                        )
                    )
                )
                await session.execute(
                    agent_grants.insert().values(
                        **_signed_values(
                            "agent_grant",
                            workspace_id=owner_binding.workspace_id,
                            agent_profile_id=(
                                owner_binding.agent_profile_id
                            ),
                            principal_id=member,
                            role=AgentGrantRole.READER.value,
                        )
                    )
                )

            reader_binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=member,
                action=MemoryAction.READ,
            )
            assert reader_binding.scope == owner_binding.scope
            assert reader_binding.agent_role is AgentGrantRole.READER

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=member,
                    action=MemoryAction.WRITE,
                )
        finally:
            await _delete_workspace(session_factory, workspace_slug)

@pytest.mark.live
async def test_live_member_cannot_create_a_missing_agent() -> None:
    suffix = uuid4().hex
    workspace_slug = f"member-create-{suffix}"
    existing_agent_slug = f"existing-{suffix}"
    missing_agent_slug = f"missing-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=owner,
            )
            owner_binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=existing_agent_slug,
                principal_id=owner,
                action=MemoryAction.READ,
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    workspace_members.insert().values(
                        **_signed_values(
                            "workspace_member",
                            workspace_id=owner_binding.workspace_id,
                            principal_id=member,
                            role=WorkspaceRole.MEMBER.value,
                        )
                    )
                )
                await session.execute(
                    agent_grants.insert().values(
                        **_signed_values(
                            "agent_grant",
                            workspace_id=owner_binding.workspace_id,
                            agent_profile_id=owner_binding.agent_profile_id,
                            principal_id=member,
                            role=AgentGrantRole.READER.value,
                        )
                    )
                )

            existing = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=existing_agent_slug,
                principal_id=member,
                action=MemoryAction.READ,
            )
            assert existing.scope == owner_binding.scope

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=missing_agent_slug,
                    principal_id=member,
                    action=MemoryAction.READ,
                )

            async with session_factory() as session:
                missing_count = (
                    await session.execute(
                        select(agent_profiles.c.agent_profile_id).where(
                            agent_profiles.c.workspace_id
                            == owner_binding.workspace_id,
                            agent_profiles.c.slug == missing_agent_slug,
                        )
                    )
                ).scalars().all()
            assert missing_count == []
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_all_members_policy_allows_atomic_mcp_agent_creation() -> None:
    suffix = uuid4().hex
    workspace_slug = f"all-members-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    member = f"oidc:tenant:member-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            workspace_id = await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=owner,
                agent_creation_policy=(
                    WorkspaceAgentCreationPolicy.ALL_MEMBERS
                ),
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    workspace_members.insert().values(
                        **_signed_values(
                            "workspace_member",
                            workspace_id=workspace_id,
                            principal_id=member,
                            role=WorkspaceRole.MEMBER.value,
                        )
                    )
                )

            binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=member,
                action=MemoryAction.DELETE,
            )
            assert binding.workspace_role is WorkspaceRole.MEMBER
            assert binding.agent_role is AgentGrantRole.ADMIN
            async with session_factory() as session:
                manager = (
                    await session.execute(
                        select(agent_managers.c.principal_id).where(
                            agent_managers.c.agent_profile_id
                            == binding.agent_profile_id
                        )
                    )
                ).scalar_one()
            assert manager == member
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_agent_creation_limit_is_enforced() -> None:
    suffix = uuid4().hex
    workspace_slug = f"bounded-{suffix}"
    agent_slug = f"agent-{suffix}"
    second_agent_slug = f"agent-second-{suffix}"
    principal = f"oidc:tenant:principal-{suffix}"

    async with _live_store() as (_store, session_factory):
        store = NamespaceStore(
            session_factory,
            integrity_key=_INTEGRITY_KEY,
            max_agents_per_workspace=1,
        )
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=principal,
            )
            first = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=principal,
                action=MemoryAction.READ,
            )
            reconnect = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=principal,
                action=MemoryAction.READ,
            )
            assert reconnect.scope == first.scope

            with pytest.raises(AuthorizationDenied):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=second_agent_slug,
                    principal_id=principal,
                    action=MemoryAction.READ,
                )
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_mcp_path_never_claims_or_creates_a_workspace() -> None:
    suffix = uuid4().hex
    workspace_slug = f"missing-{suffix}"
    principal = f"oidc:tenant:principal-{suffix}"

    async with _live_store() as (store, session_factory):
        with pytest.raises(AuthorizationDenied):
            await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=f"agent-{suffix}",
                principal_id=principal,
                action=MemoryAction.READ,
            )

        async with session_factory() as session:
            assert (
                await session.execute(
                    select(workspaces.c.workspace_id).where(
                        workspaces.c.slug == workspace_slug
                    )
                )
            ).scalar_one_or_none() is None


@pytest.mark.live
async def test_live_workspace_owner_cannot_decrypt_without_agent_grant() -> None:
    suffix = uuid4().hex
    workspace_slug = f"owner-boundary-{suffix}"
    agent_slug = f"agent-{suffix}"
    creator = f"oidc:tenant:creator-{suffix}"
    other_owner = f"oidc:tenant:other-owner-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=creator,
            )
            creator_binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=creator,
                action=MemoryAction.READ,
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    workspace_members.insert().values(
                        **_signed_values(
                            "workspace_member",
                            workspace_id=creator_binding.workspace_id,
                            principal_id=other_owner,
                            role=WorkspaceRole.OWNER.value,
                        )
                    )
                )

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=other_owner,
                    action=MemoryAction.READ,
                )

            async with session_factory() as session, session.begin():
                await session.execute(
                    agent_grants.insert().values(
                        **_signed_values(
                            "agent_grant",
                            workspace_id=creator_binding.workspace_id,
                            agent_profile_id=(
                                creator_binding.agent_profile_id
                            ),
                            principal_id=other_owner,
                            role=AgentGrantRole.READER.value,
                        )
                    )
                )

            granted = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=other_owner,
                action=MemoryAction.READ,
            )
            assert granted.scope == creator_binding.scope
            assert granted.workspace_role is WorkspaceRole.OWNER
            assert granted.agent_role is AgentGrantRole.READER
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_forged_membership_and_grant_tags_fail_closed() -> None:
    suffix = uuid4().hex
    workspace_slug = f"forged-acl-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    attacker = f"oidc:tenant:attacker-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=owner,
            )
            binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=owner,
                action=MemoryAction.READ,
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    workspace_members.insert().values(
                        workspace_id=binding.workspace_id,
                        principal_id=attacker,
                        role=WorkspaceRole.MEMBER.value,
                        integrity_version=1,
                        integrity_tag=b"x" * 32,
                    )
                )
                await session.execute(
                    agent_grants.insert().values(
                        workspace_id=binding.workspace_id,
                        agent_profile_id=binding.agent_profile_id,
                        principal_id=attacker,
                        role=AgentGrantRole.READER.value,
                        integrity_version=1,
                        integrity_tag=b"y" * 32,
                    )
                )

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=attacker,
                    action=MemoryAction.READ,
                )

            async with session_factory() as session, session.begin():
                signed_member = _signed_values(
                    "workspace_member",
                    workspace_id=binding.workspace_id,
                    principal_id=attacker,
                    role=WorkspaceRole.MEMBER.value,
                )
                await session.execute(
                    update(workspace_members)
                    .where(
                        workspace_members.c.workspace_id
                        == binding.workspace_id,
                        workspace_members.c.principal_id == attacker,
                    )
                    .values(integrity_tag=signed_member["integrity_tag"])
                )

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=attacker,
                    action=MemoryAction.READ,
                )
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_valid_tags_cannot_be_copied_to_another_principal() -> None:
    suffix = uuid4().hex
    workspace_slug = f"copied-tag-{suffix}"
    agent_slug = f"agent-{suffix}"
    owner = f"oidc:tenant:owner-{suffix}"
    attacker = f"oidc:tenant:attacker-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=owner,
            )
            binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=owner,
                action=MemoryAction.READ,
            )
            async with session_factory() as session, session.begin():
                owner_member = (
                    await session.execute(
                        select(
                            workspace_members.c.integrity_version,
                            workspace_members.c.integrity_tag,
                        ).where(
                            workspace_members.c.workspace_id
                            == binding.workspace_id,
                            workspace_members.c.principal_id == owner,
                        )
                    )
                ).one()
                owner_grant = (
                    await session.execute(
                        select(
                            agent_grants.c.integrity_version,
                            agent_grants.c.integrity_tag,
                        ).where(
                            agent_grants.c.agent_profile_id
                            == binding.agent_profile_id,
                            agent_grants.c.principal_id == owner,
                        )
                    )
                ).one()
                await session.execute(
                    workspace_members.insert().values(
                        workspace_id=binding.workspace_id,
                        principal_id=attacker,
                        role=WorkspaceRole.OWNER.value,
                        integrity_version=owner_member.integrity_version,
                        integrity_tag=owner_member.integrity_tag,
                    )
                )
                await session.execute(
                    agent_grants.insert().values(
                        workspace_id=binding.workspace_id,
                        agent_profile_id=binding.agent_profile_id,
                        principal_id=attacker,
                        role=AgentGrantRole.ADMIN.value,
                        integrity_version=owner_grant.integrity_version,
                        integrity_tag=owner_grant.integrity_tag,
                    )
                )

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=attacker,
                    action=MemoryAction.READ,
                )
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_authorization_rows_reject_a_different_integrity_key() -> None:
    suffix = uuid4().hex
    workspace_slug = f"wrong-key-{suffix}"
    agent_slug = f"agent-{suffix}"
    principal = f"oidc:tenant:principal-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=principal,
            )
            await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=principal,
                action=MemoryAction.READ,
            )
            wrong_key_store = NamespaceStore(
                session_factory,
                integrity_key=b"different-integrity-key-0123456789abcdef",
            )
            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await wrong_key_store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=principal,
                    action=MemoryAction.READ,
                )
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
@pytest.mark.parametrize(
    "record_type",
    ["workspace", "workspace_member", "agent_profile", "agent_grant"],
)
async def test_live_tampered_authorization_rows_fail_closed(
    record_type: str,
) -> None:
    suffix = uuid4().hex
    workspace_slug = f"tampered-{record_type.replace('_', '-')}-{suffix}"
    agent_slug = f"agent-{suffix}"
    principal = f"oidc:tenant:principal-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=principal,
            )
            binding = await store.resolve_or_create(
                workspace_slug=workspace_slug,
                agent_slug=agent_slug,
                principal_id=principal,
                action=MemoryAction.READ,
            )
            mutations = {
                "workspace": (
                    workspaces,
                    workspaces.c.workspace_id == binding.workspace_id,
                    {"created_by_principal_id": f"forged-{suffix}"},
                ),
                "workspace_member": (
                    workspace_members,
                    workspace_members.c.workspace_id == binding.workspace_id,
                    {"role": WorkspaceRole.MEMBER.value},
                ),
                "agent_profile": (
                    agent_profiles,
                    agent_profiles.c.agent_profile_id
                    == binding.agent_profile_id,
                    {"display_alias": "forged alias"},
                ),
                "agent_grant": (
                    agent_grants,
                    agent_grants.c.agent_profile_id
                    == binding.agent_profile_id,
                    {"role": AgentGrantRole.READER.value},
                ),
            }
            table, predicate, values = mutations[record_type]
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(table).where(predicate).values(**values)
                )

            with pytest.raises(
                AuthorizationDenied,
                match="memory operation is not authorized",
            ):
                await store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=principal,
                    action=MemoryAction.READ,
                )
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_concurrent_resolve_or_create_is_singleton() -> None:
    suffix = uuid4().hex
    workspace_slug = f"concurrent-{suffix}"
    agent_slug = f"agent-{suffix}"
    principal = f"oidc:tenant:principal-{suffix}"

    async with _live_store() as (store, session_factory):
        try:
            await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=principal,
            )
            first, second = await asyncio.gather(
                store.resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=principal,
                    action=MemoryAction.READ,
                ),
                NamespaceStore(
                    session_factory,
                    integrity_key=_INTEGRITY_KEY,
                ).resolve_or_create(
                    workspace_slug=workspace_slug,
                    agent_slug=agent_slug,
                    principal_id=principal,
                    action=MemoryAction.WRITE,
                ),
            )
            assert first.scope == second.scope
            assert first.workspace_role is WorkspaceRole.OWNER
            assert second.workspace_role is WorkspaceRole.OWNER
            assert first.agent_role is AgentGrantRole.ADMIN
            assert second.agent_role is AgentGrantRole.ADMIN
        finally:
            await _delete_workspace(session_factory, workspace_slug)


@pytest.mark.live
async def test_live_concurrent_agent_claim_has_one_creator_grant_and_manager() -> None:
    suffix = uuid4().hex
    workspace_slug = f"claim-{suffix}"
    agent_slug = f"agent-{suffix}"
    principals = (
        f"oidc:tenant:first-{suffix}",
        f"oidc:tenant:second-{suffix}",
    )

    async with _live_store() as (_store, session_factory):
        try:
            workspace_id = await _seed_workspace(
                session_factory,
                workspace_slug=workspace_slug,
                principal_id=principals[0],
            )
            async with session_factory() as session, session.begin():
                await session.execute(
                    workspace_members.insert().values(
                        **_signed_values(
                            "workspace_member",
                            workspace_id=workspace_id,
                            principal_id=principals[1],
                            role=WorkspaceRole.ADMIN.value,
                        )
                    )
                )
            results = await asyncio.gather(
                *(
                    NamespaceStore(
                        session_factory,
                        integrity_key=_INTEGRITY_KEY,
                    ).resolve_or_create(
                        workspace_slug=workspace_slug,
                        agent_slug=agent_slug,
                        principal_id=principal,
                        action=MemoryAction.READ,
                    )
                    for principal in principals
                ),
                return_exceptions=True,
            )
            successes = [
                result
                for result in results
                if not isinstance(result, BaseException)
            ]
            failures = [
                result for result in results if isinstance(result, BaseException)
            ]
            assert len(successes) == 1
            assert len(failures) == 1
            assert isinstance(failures[0], AuthorizationDenied)

            async with session_factory() as session:
                profile_id = (
                    await session.execute(
                        select(agent_profiles.c.agent_profile_id).where(
                            agent_profiles.c.workspace_id == workspace_id,
                            agent_profiles.c.slug == agent_slug,
                        )
                    )
                ).scalar_one()
                grant_principals = set(
                    (
                        await session.execute(
                            select(agent_grants.c.principal_id).where(
                                agent_grants.c.agent_profile_id == profile_id
                            )
                        )
                    ).scalars()
                )
                manager_principals = set(
                    (
                        await session.execute(
                            select(agent_managers.c.principal_id).where(
                                agent_managers.c.agent_profile_id == profile_id
                            )
                        )
                    ).scalars()
                )
            assert grant_principals == manager_principals == {
                successes[0].principal_id
            }
        finally:
            await _delete_workspace(session_factory, workspace_slug)
