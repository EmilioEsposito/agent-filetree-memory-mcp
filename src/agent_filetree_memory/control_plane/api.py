"""Authenticated REST control plane for the companion memory manager UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.domain.errors import (
    AuthorizationDenied,
    IdempotencyConflict,
    IntegrityFailure,
    InvalidMemoryPath,
    NotFoundOrDenied,
    QuotaExceeded,
    RateLimitExceeded,
    VersionConflict,
)
from agent_filetree_memory.domain.models import MemoryAction, VerifiedInvocation
from fastapi import Body, Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field


from .management_store import (
    ManagementConflict,
    ManagementStore,
    SelfGrantDisabled,
    content_role_from_name,
    content_role_name,
)
from .namespace_store import NamespaceStore, WorkspaceRole

_INVOCATION_TTL = timedelta(seconds=60)
_MANAGEMENT_ISSUER = "agent-filetree-memory-manager"
_MANAGEMENT_AUDIENCE = "agent-filetree-memory"


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    principal_id: str
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class LocalManagementScope:
    workspace_id: str
    workspace_slug: str
    agent_profile_id: str
    agent_slug: str
    display_alias: str


class CreateWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=63)


class CreateAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(min_length=1, max_length=63)
    display_alias: str | None = Field(default=None, max_length=128)


class UpdateAgentAliasRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_alias: str = Field(min_length=1, max_length=128)


class InviteMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    role: Literal["admin", "member"] = "member"


class PrincipalTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_principal_id: str = Field(min_length=1, max_length=255)


class UpdateMemberRoleRequest(PrincipalTargetRequest):
    role: Literal["admin", "member"]


class SetContentAccessRequest(PrincipalTargetRequest):
    content_role: Literal["reader", "editor", "full_access"] | None


class SetManagerRequest(PrincipalTargetRequest):
    enabled: bool


class WriteDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_048_576)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


class AppendDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(min_length=1, max_length=262_144)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


class DeleteDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


def _workspace_payload(item) -> dict[str, object]:
    return {
        "workspace_id": item.workspace_id,
        "slug": item.slug,
        "role": item.role.value,
        "agent_count": item.agent_count,
        "member_count": item.member_count,
        "created_at": item.created_at.isoformat(),
    }


def _agent_payload(item) -> dict[str, object]:
    return {
        "agent_profile_id": item.agent_profile_id,
        "slug": item.slug,
        "display_alias": item.display_alias,
        "content_role": content_role_name(item.content_role),
        "can_manage": item.can_manage,
        "created_at": item.created_at.isoformat(),
    }


def _member_payload(item) -> dict[str, object]:
    return {
        "principal_id": item.principal_id,
        "email": item.email,
        "display_name": item.display_name,
        "workspace_role": item.workspace_role.value,
        "content_role": content_role_name(item.content_role),
        "explicit_manager": item.explicit_manager,
    }


def create_management_api(
    *,
    management_store: ManagementStore,
    namespace_store: NamespaceStore,
    memory_service: MemoryService,
    principal_dependency: Callable[..., Any],
    allow_admin_self_grant: bool,
    local_scope: LocalManagementScope | None = None,
) -> FastAPI:
    """Build the management API around one host-supplied identity dependency."""

    if not callable(principal_dependency):
        raise TypeError("principal_dependency must be callable")
    if local_scope is not None and not isinstance(
        local_scope, LocalManagementScope
    ):
        raise TypeError("local_scope must be LocalManagementScope")

    app = FastAPI(
        title="Agent Filetree Memory API",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    async def current_principal(
        principal: ManagementPrincipal = Depends(principal_dependency),
    ) -> ManagementPrincipal:
        if not isinstance(principal, ManagementPrincipal):
            raise AuthorizationDenied("management authentication is invalid")
        await management_store.register_principal(
            principal_id=principal.principal_id,
            email=principal.email,
            display_name=principal.display_name,
        )
        if local_scope is not None:
            await management_store.ensure_local_scope(
                principal_id=principal.principal_id,
                workspace_id=local_scope.workspace_id,
                workspace_slug=local_scope.workspace_slug,
                agent_profile_id=local_scope.agent_profile_id,
                agent_slug=local_scope.agent_slug,
                display_alias=local_scope.display_alias,
            )
        return principal

    @app.exception_handler(SelfGrantDisabled)
    async def self_grant_disabled_handler(_request, exc: SelfGrantDisabled):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(AuthorizationDenied)
    async def authorization_handler(_request, _exc: AuthorizationDenied):
        return JSONResponse(
            {"detail": "management operation is not authorized"},
            status_code=403,
        )

    @app.exception_handler(ManagementConflict)
    async def conflict_handler(_request, exc: ManagementConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(NotFoundOrDenied)
    async def missing_handler(_request, _exc: NotFoundOrDenied):
        return JSONResponse(
            {"detail": "memory is unavailable"},
            status_code=404,
        )

    async def version_handler(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    app.add_exception_handler(VersionConflict, version_handler)
    app.add_exception_handler(IdempotencyConflict, version_handler)

    @app.exception_handler(QuotaExceeded)
    async def quota_handler(_request, exc: QuotaExceeded):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RateLimitExceeded)
    async def rate_handler(_request, exc: RateLimitExceeded):
        return JSONResponse({"detail": str(exc)}, status_code=429)

    @app.exception_handler(InvalidMemoryPath)
    async def path_handler(_request, exc: InvalidMemoryPath):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(IntegrityFailure)
    async def integrity_handler(_request, _exc: IntegrityFailure):
        return JSONResponse(
            {"detail": "encrypted memory could not be verified"},
            status_code=503,
        )

    async def invocation_for(
        *,
        principal: ManagementPrincipal,
        workspace_slug: str,
        agent_slug: str,
        action: MemoryAction,
    ) -> VerifiedInvocation:
        binding = await namespace_store.resolve_or_create(
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            principal_id=principal.principal_id,
            action=action,
            display_alias=agent_slug,
        )
        now = datetime.now(timezone.utc)
        return VerifiedInvocation(
            scope=binding.scope,
            principal_id=principal.principal_id,
            invocation_id=uuid4().hex,
            capability_id=uuid4().hex,
            issuer=_MANAGEMENT_ISSUER,
            audience=_MANAGEMENT_AUDIENCE,
            allowed_actions=frozenset({action}),
            issued_at=now,
            expires_at=now + _INVOCATION_TTL,
        )

    @app.get("/me")
    async def me(
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        return {
            "principal_id": principal.principal_id,
            "email": principal.email,
            "display_name": principal.display_name,
            "allow_admin_self_grant": allow_admin_self_grant,
        }

    @app.get("/policy")
    async def policy(
        _principal: ManagementPrincipal = Depends(current_principal),
    ):
        return {
            "allow_admin_self_grant": allow_admin_self_grant,
            "workspace_admins_list_all_agents": True,
            "management_implies_content_access": False,
            "content_roles": ["reader", "editor", "full_access"],
        }

    @app.get("/workspaces")
    async def list_workspaces(
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        items = await management_store.list_workspaces(
            principal_id=principal.principal_id
        )
        return {"workspaces": [_workspace_payload(item) for item in items]}

    @app.post("/workspaces", status_code=201)
    async def create_workspace(
        body: CreateWorkspaceRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        item = await management_store.create_workspace(
            principal_id=principal.principal_id,
            workspace_slug=body.slug,
        )
        return _workspace_payload(item)

    @app.get("/workspaces/{workspace_slug}/agents")
    async def list_agents(
        workspace_slug: str,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        items = await management_store.list_agents(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
        )
        return {"agents": [_agent_payload(item) for item in items]}

    @app.post(
        "/workspaces/{workspace_slug}/agents",
        status_code=201,
    )
    async def create_agent(
        workspace_slug: str,
        body: CreateAgentRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        item = await management_store.create_agent(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=body.slug,
            display_alias=body.display_alias,
        )
        return _agent_payload(item)

    @app.patch("/workspaces/{workspace_slug}/agents/{agent_slug}")
    async def update_agent(
        workspace_slug: str,
        agent_slug: str,
        body: UpdateAgentAliasRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        item = await management_store.update_agent_alias(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            display_alias=body.display_alias,
        )
        return _agent_payload(item)

    @app.get("/workspaces/{workspace_slug}/members")
    async def list_members(
        workspace_slug: str,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        members, invitations = await management_store.list_members(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
        )
        return {
            "members": [_member_payload(item) for item in members],
            "invitations": [
                {
                    "invitation_id": item.invitation_id,
                    "email": item.email,
                    "role": item.role.value,
                    "invited_by_principal_id": (
                        item.invited_by_principal_id
                    ),
                    "created_at": item.created_at.isoformat(),
                }
                for item in invitations
            ],
        }

    @app.post("/workspaces/{workspace_slug}/members", status_code=201)
    async def invite_member(
        workspace_slug: str,
        body: InviteMemberRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        result = await management_store.invite_member(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            email=body.email,
            role=WorkspaceRole(body.role),
        )
        return {"created": result}

    @app.delete(
        "/workspaces/{workspace_slug}/invitations/{invitation_id}",
        status_code=204,
    )
    async def revoke_invitation(
        workspace_slug: str,
        invitation_id: str,
        principal: ManagementPrincipal = Depends(current_principal),
    ) -> None:
        await management_store.revoke_invitation(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            invitation_id=invitation_id,
        )

    @app.put("/workspaces/{workspace_slug}/members/role")
    async def update_member_role(
        workspace_slug: str,
        body: UpdateMemberRoleRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        await management_store.update_member_role(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            target_principal_id=body.target_principal_id,
            role=WorkspaceRole(body.role),
        )
        return {"updated": True}

    @app.post("/workspaces/{workspace_slug}/transfer-ownership")
    async def transfer_ownership(
        workspace_slug: str,
        body: PrincipalTargetRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        await management_store.transfer_ownership(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            target_principal_id=body.target_principal_id,
        )
        return {"transferred": True}

    @app.delete("/workspaces/{workspace_slug}/members")
    async def remove_member(
        workspace_slug: str,
        body: PrincipalTargetRequest = Body(),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        await management_store.remove_member(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            target_principal_id=body.target_principal_id,
        )
        return {"removed": True}

    @app.get(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/access"
    )
    async def list_agent_access(
        workspace_slug: str,
        agent_slug: str,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        items = await management_store.list_agent_access(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
        )
        return {"members": [_member_payload(item) for item in items]}

    @app.put(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/content-access"
    )
    async def set_content_access(
        workspace_slug: str,
        agent_slug: str,
        body: SetContentAccessRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        await management_store.set_content_access(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            target_principal_id=body.target_principal_id,
            role=(
                content_role_from_name(body.content_role)
                if body.content_role is not None
                else None
            ),
            allow_admin_self_grant=allow_admin_self_grant,
        )
        return {"updated": True}

    @app.put(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/manager"
    )
    async def set_agent_manager(
        workspace_slug: str,
        agent_slug: str,
        body: SetManagerRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        await management_store.set_agent_manager(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            target_principal_id=body.target_principal_id,
            enabled=body.enabled,
        )
        return {"updated": True}

    @app.get("/workspaces/{workspace_slug}/audit")
    async def list_audit(
        workspace_slug: str,
        limit: int = Query(default=100, ge=1, le=200),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        items = await management_store.list_audit_events(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            limit=limit,
        )
        return {
            "events": [
                {
                    "event_id": item.event_id,
                    "actor_principal_id": item.actor_principal_id,
                    "action": item.action,
                    "target_kind": item.target_kind,
                    "target_id": item.target_id,
                    "occurred_at": item.occurred_at.isoformat(),
                }
                for item in items
            ]
        }

    @app.get(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory"
    )
    async def list_memory(
        workspace_slug: str,
        agent_slug: str,
        path: str = Query(default="/", min_length=1, max_length=4096),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.LIST,
        )
        entries = await memory_service.list(invocation, path)
        return {
            "path": path,
            "entries": [
                {
                    "name": item.name,
                    "path": item.path,
                    "kind": item.kind,
                    "version": item.version,
                    "updated_at": item.updated_at.isoformat(),
                }
                for item in entries
            ],
        }

    @app.get(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory/document"
    )
    async def read_document(
        workspace_slug: str,
        agent_slug: str,
        path: str = Query(min_length=1, max_length=4096),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.READ,
        )
        item = await memory_service.read(invocation, path)
        return {
            "path": item.path,
            "content": item.content,
            "version": item.version,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }

    @app.put(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory/document"
    )
    async def write_document(
        workspace_slug: str,
        agent_slug: str,
        body: WriteDocumentRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.WRITE,
        )
        item = await memory_service.write(
            invocation,
            body.path,
            body.content,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
        )
        return {
            "path": item.path,
            "version": item.version,
            "created": item.created,
            "idempotent_replay": item.idempotent_replay,
        }

    @app.post(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory/append"
    )
    async def append_document(
        workspace_slug: str,
        agent_slug: str,
        body: AppendDocumentRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.APPEND,
        )
        item = await memory_service.append(
            invocation,
            body.path,
            body.content,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
        )
        return {
            "path": item.path,
            "version": item.version,
            "created": item.created,
            "idempotent_replay": item.idempotent_replay,
        }

    @app.delete(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory/document"
    )
    async def delete_document(
        workspace_slug: str,
        agent_slug: str,
        body: DeleteDocumentRequest = Body(),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.DELETE,
        )
        item = await memory_service.delete(
            invocation,
            body.path,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
        )
        return {
            "path": item.path,
            "deleted_version": item.deleted_version,
            "purge_after": item.purge_after.isoformat(),
            "idempotent_replay": item.idempotent_replay,
        }

    return app


__all__ = [
    "LocalManagementScope",
    "ManagementPrincipal",
    "create_management_api",
]
