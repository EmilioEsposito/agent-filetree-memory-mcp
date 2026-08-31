from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException
from fastapi.testclient import TestClient

from agent_filetree_memory.control_plane.api import (
    LocalManagementScope,
    ManagementPrincipal,
    create_management_api,
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
        "allow_admin_self_grant": True,
    }
    assert policy.json() == {
        "allow_admin_self_grant": True,
        "workspace_admins_list_all_agents": True,
        "management_implies_content_access": False,
        "content_roles": ["reader", "editor", "full_access"],
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
