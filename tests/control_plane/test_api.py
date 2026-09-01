from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated

from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from agent_filetree_memory.control_plane.api import (
    LocalManagementScope,
    ManagementPrincipal,
    create_management_api,
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

    async def register_principal(self, **kwargs) -> None:
        self.calls.append(("principal", kwargs))

    async def ensure_local_scope(self, **kwargs) -> None:
        self.calls.append(("scope", kwargs))


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
        "allow_admin_self_grant": True,
    }
    assert policy.json() == {
        "allow_admin_self_grant": True,
        "workspace_admins_list_all_agents": True,
        "management_implies_content_access": False,
        "content_roles": ["reader", "editor", "full_access"],
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
