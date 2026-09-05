from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated

import pytest

from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from agent_filetree_memory.control_plane.api import (
    LocalManagementScope,
    ManagementPrincipal,
    create_management_api,
)
from agent_filetree_memory.control_plane.namespace_store import (
    AgentAccessPolicy,
    AgentGrantRole,
)
from agent_filetree_memory.domain.models import (
    HistoricalDocument,
    MemoryAction,
    MemoryHistoryPage,
    MemoryVersion,
    Scope,
)


class _ManagementStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def workspace_creation_usage(self, **kwargs):
        return 0, 10

    async def register_principal(self, **kwargs) -> None:
        self.calls.append(("principal", kwargs))

    async def ensure_local_scope(self, **kwargs) -> None:
        self.calls.append(("scope", kwargs))

    async def set_agent_access_policy(self, **kwargs):
        self.calls.append(("agent-access-policy", kwargs))
        return SimpleNamespace(
            agent_profile_id="agent-1",
            slug=kwargs["agent_slug"],
            display_alias="Example agent",
            content_role=AgentGrantRole.READER,
            explicit_content_role=None,
            access_policy=kwargs["access_policy"],
            can_manage=True,
            created_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
        )


async def _principal(
    authorization: Annotated[str | None, Header()] = None,
) -> ManagementPrincipal:
    if authorization != "Bearer verified":
        raise HTTPException(status_code=401, detail="authentication required")
    return ManagementPrincipal(
        principal_id="oidc:tenant:person",
        email="person@example.test",
        display_name="Example person",
    )


def test_management_api_uses_injected_identity_and_separate_policy() -> None:
    store = _ManagementStore()
    app = create_management_api(
        management_store=store,
        namespace_store=object(),
        memory_service=object(),
        principal_dependency=_principal,
        allow_admin_self_grant=True,
    )
    with TestClient(app) as client:
        denied = client.get("/me")
        response = client.get(
            "/me", headers={"Authorization": "Bearer verified"}
        )
        policy = client.get(
            "/policy", headers={"Authorization": "Bearer verified"}
        )

    assert denied.status_code == 401
    assert response.json() == {
        "principal_id": "oidc:tenant:person",
        "email": "person@example.test",
        "display_name": "Example person",
        "is_platform_admin": False,
        "can_create_workspaces": False,
        "auto_create_personal_workspace": False,
        "workspace_creation_restriction": "policy",
        "created_workspace_count": 0,
        "workspace_creation_limit": 10,
        "allow_admin_self_grant": True,
    }
    assert policy.json() == {
        "allow_admin_self_grant": True,
        "workspace_admins_list_all_agents": True,
        "management_implies_content_access": False,
        "content_roles": ["reader", "editor", "full_access"],
        "agent_access_policies": ["private", "workspace_read"],
        "workspace_admission_policies": [
            "invite_only",
            "all_authenticated",
            "external_entitlement",
        ],
        "workspace_agent_creation_policies": [
            "admins_only",
            "all_members",
        ],
    }
    assert store.calls[0][0] == "principal"


def test_agent_access_policy_endpoint_requires_an_explicit_policy() -> None:
    store = _ManagementStore()
    app = create_management_api(
        management_store=store,
        namespace_store=object(),
        memory_service=object(),
        principal_dependency=_principal,
        allow_admin_self_grant=True,
    )
    headers = {"Authorization": "Bearer verified"}
    with TestClient(app) as client:
        response = client.put(
            "/workspaces/team/agents/assistant/access-policy",
            headers=headers,
            json={
                "access_policy": "workspace_read",
                "confirm_self_grant": True,
            },
        )
        invalid = client.put(
            "/workspaces/team/agents/assistant/access-policy",
            headers=headers,
            json={"access_policy": "workspace_write"},
        )

    assert response.status_code == 200
    assert response.json()["access_policy"] == "workspace_read"
    assert response.json()["content_role"] == "reader"
    assert response.json()["explicit_content_role"] is None
    assert invalid.status_code == 422
    assert (
        "agent-access-policy",
        {
            "principal_id": "oidc:tenant:person",
            "workspace_slug": "team",
            "agent_slug": "assistant",
            "access_policy": AgentAccessPolicy.WORKSPACE_READ,
            "allow_admin_self_grant": True,
            "self_grant_confirmed": True,
        },
    ) in store.calls


def test_local_scope_bootstrap_is_explicit_and_idempotent_by_store() -> None:
    store = _ManagementStore()

    async def local_principal() -> ManagementPrincipal:
        return ManagementPrincipal(
            principal_id="local-person",
            email="local@example.test",
            display_name="Local person",
        )

    app = create_management_api(
        management_store=store,
        namespace_store=object(),
        memory_service=object(),
        principal_dependency=local_principal,
        allow_admin_self_grant=False,
        local_scope=LocalManagementScope(
            workspace_id="workspace-opaque",
            workspace_slug="workspace-one",
            agent_profile_id="agent-opaque",
            agent_slug="agent-one",
            display_alias="Agent one",
        ),
    )
    with TestClient(app) as client:
        response = client.get("/me")

    assert response.status_code == 200
    assert store.calls == [
        (
            "principal",
            {
                "principal_id": "local-person",
                "email": "local@example.test",
                "display_name": "Local person",
            },
        ),
        (
            "scope",
            {
                "principal_id": "local-person",
                "workspace_id": "workspace-opaque",
                "workspace_slug": "workspace-one",
                "agent_profile_id": "agent-opaque",
                "agent_slug": "agent-one",
                "display_alias": "Agent one",
            },
        ),
    ]


def test_management_history_endpoints_keep_actions_and_attribution_distinct() -> None:
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    class Namespace:
        def __init__(self) -> None:
            self.actions: list[MemoryAction] = []

        async def resolve_or_create(self, *, action: MemoryAction, **_kwargs):
            self.actions.append(action)
            return SimpleNamespace(scope=Scope("workspace-1", "agent-1"))

    class Memory:
        async def list_history(self, invocation, path, **_kwargs):
            invocation.require(MemoryAction.HISTORY_LIST)
            return MemoryHistoryPage(
                path=path,
                current_version=2,
                versions=(
                    MemoryVersion(
                        version=2,
                        version_created_at=now,
                        committed_by_principal_id="oidc:tenant:person",
                        co_authored_by=("agent:claude",),
                        change_comment="Explain the revision",
                    ),
                ),
            )

        async def read_history(
            self,
            invocation,
            path,
            version,
            *,
            compare_to_version=None,
        ):
            invocation.require(MemoryAction.HISTORY_READ)
            return HistoricalDocument(
                path=path,
                content="# Version two",
                version=version,
                version_created_at=now,
                committed_by_principal_id="oidc:tenant:person",
                co_authored_by=("agent:claude",),
                change_comment="Explain the revision",
                compared_to_version=compare_to_version,
                diff="--- v1\n+++ v2\n",
            )

    management = _ManagementStore()
    namespace = Namespace()
    app = create_management_api(
        management_store=management,
        namespace_store=namespace,
        memory_service=Memory(),
        principal_dependency=_principal,
        allow_admin_self_grant=False,
    )
    headers = {"Authorization": "Bearer verified"}
    base = "/workspaces/team/agents/assistant/memory/history"

    with TestClient(app) as client:
        listed = client.get(base, params={"path": "/notes.md"}, headers=headers)
        read = client.get(
            base + "/document",
            params={
                "path": "/notes.md",
                "version": 2,
                "compare_to_version": 1,
            },
            headers=headers,
        )

    assert listed.status_code == 200
    version = listed.json()["versions"][0]
    assert version["version_created_at"] == now.isoformat()
    assert version["committed_by"] == {
        "principal_id": "oidc:tenant:person",
        "verification": "authenticated",
    }
    assert version["co_authored_by"] == [
        {"identifier": "agent:claude", "verification": "self_asserted"}
    ]
    assert version["change_comment"] == "Explain the revision"
    assert read.status_code == 200
    assert read.json()["content"] == "# Version two"
    assert read.json()["change_comment"] == "Explain the revision"
    assert read.json()["compared_to_version"] == 1
    assert namespace.actions == [
        MemoryAction.HISTORY_LIST,
        MemoryAction.HISTORY_READ,
    ]


@pytest.mark.parametrize(
    "allowed,created,expected,restriction",
    [
        (False, 0, False, "policy"),
        (True, 0, True, None),
        (True, 1, False, "quota"),
    ],
)
def test_creation_capability_is_separate_from_platform_admin(
    allowed, created, expected, restriction
):
    class Store(_ManagementStore):
        async def workspace_creation_usage(self, **kwargs):
            return created, 1

    async def identity():
        return ManagementPrincipal(
            "person", "person@example.test", "Person", can_create_workspaces=allowed
        )

    app = create_management_api(
        management_store=Store(),
        namespace_store=object(),
        memory_service=object(),
        principal_dependency=identity,
        allow_admin_self_grant=False,
    )
    with TestClient(app) as client:
        me = client.get("/me").json()
        assert me["is_platform_admin"] is False
        assert me["can_create_workspaces"] is expected
        assert me["workspace_creation_restriction"] == restriction
        if not allowed:
            assert client.post("/workspaces", json={"slug": "mine"}).status_code == 403
        # A client cannot grant itself either trusted identity permission through JSON.
        assert (
            client.post(
                "/workspaces", json={"slug": "mine", "can_create_workspaces": True}
            ).status_code
            == 422
        )


def test_invalid_creation_capability_fails_closed():
    async def identity():
        return ManagementPrincipal(
            "person", "person@example.test", "Person", can_create_workspaces="true"
        )

    app = create_management_api(
        management_store=_ManagementStore(),
        namespace_store=object(),
        memory_service=object(),
        principal_dependency=identity,
        allow_admin_self_grant=False,
    )
    with TestClient(app) as client:
        assert client.get("/me").status_code == 403


@pytest.mark.parametrize(
    "automatic,allowed", [(False, True), (True, False), (True, True)]
)
def test_personal_workspace_bootstrap_requires_trusted_host_policy(automatic, allowed):
    class Store(_ManagementStore):
        async def ensure_personal_workspace(self, **kwargs):
            assert kwargs["can_create_workspaces"] is allowed
            if not allowed:
                from agent_filetree_memory.domain.errors import AuthorizationDenied

                raise AuthorizationDenied("denied")
            return None

    async def identity():
        return ManagementPrincipal(
            "person",
            "person@example.test",
            "Person",
            can_create_workspaces=allowed,
            auto_create_personal_workspace=automatic,
        )

    app = create_management_api(
        management_store=Store(),
        namespace_store=object(),
        memory_service=object(),
        principal_dependency=identity,
        allow_admin_self_grant=False,
    )
    with TestClient(app) as client:
        me = client.get("/me").json()
        assert me["auto_create_personal_workspace"] is (automatic and allowed)
        response = client.post("/onboarding/personal-workspace")
        assert response.status_code == (200 if automatic and allowed else 403)
