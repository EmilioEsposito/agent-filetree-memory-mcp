"""Authenticated REST control plane for the companion memory manager UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Callable, Sequence
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
    SelfGrantConfirmationRequired,
    SelfGrantDisabled,
    content_role_from_name,
    content_role_name,
)
from .namespace_store import (
    AgentAccessPolicy,
    NamespaceStore,
    WorkspaceAdmissionPolicy,
    WorkspaceAgentCreationPolicy,
    WorkspaceRole,
    validate_slug,
)

_INVOCATION_TTL = timedelta(seconds=60)
_MANAGEMENT_ISSUER = "agent-filetree-memory-manager"
_MANAGEMENT_AUDIENCE = "agent-filetree-memory"


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    principal_id: str
    email: str
    display_name: str
    is_platform_admin: bool = False
    # Trusted host policy: permission to create an owned workspace, never global administration.
    can_create_workspaces: bool = False
    auto_create_personal_workspace: bool = False


@dataclass(frozen=True, slots=True)
class DefaultWorkspace:
    """Trusted host configuration for a lazily created default workspace."""

    slug: str
    admission_policy: WorkspaceAdmissionPolicy
    agent_creation_policy: WorkspaceAgentCreationPolicy

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "slug",
            validate_slug(self.slug, field="workspace_slug"),
        )
        object.__setattr__(
            self,
            "admission_policy",
            WorkspaceAdmissionPolicy(self.admission_policy),
        )
        object.__setattr__(
            self,
            "agent_creation_policy",
            WorkspaceAgentCreationPolicy(self.agent_creation_policy),
        )


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
    admission_policy: Literal[
        "invite_only", "all_authenticated", "external_entitlement"
    ] = "invite_only"
    agent_creation_policy: Literal["admins_only", "all_members"] = (
        "admins_only"
    )


class UpdateWorkspacePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    admission_policy: Literal[
        "invite_only", "all_authenticated", "external_entitlement"
    ]
    agent_creation_policy: Literal["admins_only", "all_members"]


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
    confirm_self_grant: bool = False


class SetAgentAccessPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    access_policy: Literal["private", "workspace_read"]
    confirm_self_grant: bool = False


class SetManagerRequest(PrincipalTargetRequest):
    enabled: bool


class WriteDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_048_576)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    co_authored_by: list[str] = Field(default_factory=list, max_length=8)
    change_comment: str | None = Field(default=None, max_length=2048)


class AppendDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(min_length=1, max_length=262_144)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)
    co_authored_by: list[str] = Field(default_factory=list, max_length=8)
    change_comment: str | None = Field(default=None, max_length=2048)


class DeleteDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=4096)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=255)


def _workspace_payload(item) -> dict[str, object]:
    return {
        "workspace_id": item.workspace_id,
        "slug": item.slug,
        "role": item.role.value if item.role is not None else None,
        "admission_policy": item.admission_policy.value,
        "agent_creation_policy": item.agent_creation_policy.value,
        "can_create_agents": item.can_create_agents,
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
        "explicit_content_role": content_role_name(
            item.explicit_content_role
        ),
        "access_policy": item.access_policy.value,
        "can_manage": item.can_manage,
        "created_at": item.created_at.isoformat(),
    }


def _version_attribution_payload(item) -> dict[str, object]:
    return {
        "committed_by": (
            {
                "principal_id": item.committed_by_principal_id,
                "verification": "authenticated",
            }
            if item.committed_by_principal_id is not None
            else None
        ),
        "co_authored_by": [
            {"identifier": value, "verification": "self_asserted"}
            for value in item.co_authored_by
        ],
    }


def _member_payload(item) -> dict[str, object]:
    return {
        "principal_id": item.principal_id,
        "email": item.email,
        "display_name": item.display_name,
        "workspace_role": item.workspace_role.value,
        "content_role": content_role_name(item.content_role),
        "effective_content_role": content_role_name(
            item.effective_content_role
        ),
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
    default_workspaces: Sequence[DefaultWorkspace] = (),
) -> FastAPI:
    """Build the management API around one host-supplied identity dependency."""

    if not callable(principal_dependency):
        raise TypeError("principal_dependency must be callable")
    if local_scope is not None and not isinstance(
        local_scope, LocalManagementScope
    ):
        raise TypeError("local_scope must be LocalManagementScope")
    if any(
        not isinstance(item, DefaultWorkspace)
        for item in default_workspaces
    ):
        raise TypeError("default_workspaces must contain DefaultWorkspace values")
    default_workspaces = tuple(default_workspaces)

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
        if not all(isinstance(value, bool) for value in (
            principal.is_platform_admin,
            principal.can_create_workspaces,
            principal.auto_create_personal_workspace,
        )):
            raise AuthorizationDenied("management authentication is invalid")
        profile = await management_store.register_principal(
            principal_id=principal.principal_id,
            email=principal.email,
            display_name=principal.display_name,
        )
        for workspace in default_workspaces:
            if principal.is_platform_admin:
                await management_store.ensure_default_workspace(
                    principal_id=principal.principal_id,
                    workspace_slug=workspace.slug,
                    admission_policy=workspace.admission_policy,
                    agent_creation_policy=workspace.agent_creation_policy,
                    is_platform_admin=True,
                )
            try:
                await management_store.ensure_workspace_admission(
                    principal_id=profile.principal_id,
                    email=profile.email,
                    display_name=profile.display_name,
                    workspace_slug=workspace.slug,
                )
            except AuthorizationDenied:
                # A missing, invite-only, or external-entitlement default must
                # not turn authentication into implicit membership.
                pass
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

    @app.exception_handler(SelfGrantConfirmationRequired)
    async def self_grant_confirmation_handler(
        _request, exc: SelfGrantConfirmationRequired
    ):
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
        created, limit = await management_store.workspace_creation_usage(
            principal_id=principal.principal_id,
        )
        permitted = principal.is_platform_admin or principal.can_create_workspaces
        return {
            "principal_id": principal.principal_id,
            "email": principal.email,
            "display_name": principal.display_name,
            "is_platform_admin": principal.is_platform_admin,
            "can_create_workspaces": permitted and created < limit,
            "auto_create_personal_workspace": (
                principal.auto_create_personal_workspace and permitted and created == 0 and limit > 0
            ),
            "workspace_creation_restriction": (
                "policy" if not permitted else "quota" if created >= limit else None
            ),
            "created_workspace_count": created,
            "workspace_creation_limit": limit,
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

    @app.get("/workspaces")
    async def list_workspaces(
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        items = await management_store.list_workspaces(
            principal_id=principal.principal_id,
            is_platform_admin=principal.is_platform_admin,
        )
        return {"workspaces": [_workspace_payload(item) for item in items]}

    @app.post("/onboarding/personal-workspace")
    async def provision_personal_workspace(
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        if not principal.auto_create_personal_workspace:
            raise AuthorizationDenied("Automatic workspace creation is disabled")
        item = await management_store.ensure_personal_workspace(
            principal_id=principal.principal_id,
            is_platform_admin=principal.is_platform_admin,
            can_create_workspaces=principal.can_create_workspaces,
        )
        return {"workspace": _workspace_payload(item) if item else None}

    @app.post("/workspaces", status_code=201)
    async def create_workspace(
        body: CreateWorkspaceRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        if not (principal.is_platform_admin or principal.can_create_workspaces):
            raise AuthorizationDenied("Workspace creation is restricted by this deployment")
        item = await management_store.create_workspace(
            principal_id=principal.principal_id,
            workspace_slug=body.slug,
            admission_policy=WorkspaceAdmissionPolicy(
                body.admission_policy
            ),
            agent_creation_policy=WorkspaceAgentCreationPolicy(
                body.agent_creation_policy
            ),
            is_platform_admin=principal.is_platform_admin,
            can_create_workspaces=principal.can_create_workspaces,
        )
        return _workspace_payload(item)

    @app.post("/workspaces/{workspace_slug}/join")
    async def join_workspace(
        workspace_slug: str,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        role = await management_store.ensure_workspace_admission(
            principal_id=principal.principal_id,
            email=principal.email,
            display_name=principal.display_name,
            workspace_slug=workspace_slug,
        )
        return {"role": role.value}

    @app.post("/workspaces/{workspace_slug}/platform-admin-role")
    async def assign_platform_admin_role(
        workspace_slug: str,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        item = await management_store.assign_platform_admin_role(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            is_platform_admin=principal.is_platform_admin,
        )
        return _workspace_payload(item)

    @app.put("/workspaces/{workspace_slug}/policy")
    async def update_workspace_policy(
        workspace_slug: str,
        body: UpdateWorkspacePolicyRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        item = await management_store.update_workspace_policy(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            admission_policy=WorkspaceAdmissionPolicy(
                body.admission_policy
            ),
            agent_creation_policy=WorkspaceAgentCreationPolicy(
                body.agent_creation_policy
            ),
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

    @app.put(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/access-policy"
    )
    async def set_agent_access_policy(
        workspace_slug: str,
        agent_slug: str,
        body: SetAgentAccessPolicyRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        item = await management_store.set_agent_access_policy(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            access_policy=AgentAccessPolicy(body.access_policy),
            allow_admin_self_grant=allow_admin_self_grant,
            self_grant_confirmed=body.confirm_self_grant,
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
            self_grant_confirmed=body.confirm_self_grant,
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

    @app.post(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/transfer-management"
    )
    async def transfer_agent_management(
        workspace_slug: str,
        agent_slug: str,
        body: PrincipalTargetRequest,
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        await management_store.transfer_agent_management(
            principal_id=principal.principal_id,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            target_principal_id=body.target_principal_id,
        )
        return {"transferred": True}

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
                    "version_created_at": item.version_created_at.isoformat(),
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
            "version_created_at": item.version_created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            **_version_attribution_payload(item),
            "change_comment": item.change_comment,
        }

    @app.get(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory/history"
    )
    async def list_document_history(
        workspace_slug: str,
        agent_slug: str,
        path: str = Query(min_length=1, max_length=4096),
        limit: int = Query(default=20, ge=1, le=100),
        before_version: int | None = Query(default=None, ge=1),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.HISTORY_LIST,
        )
        page = await memory_service.list_history(
            invocation,
            path,
            limit=limit,
            before_version=before_version,
        )
        return {
            "path": page.path,
            "current_version": page.current_version,
            "versions": [
                {
                    "version": item.version,
                    "version_created_at": item.version_created_at.isoformat(),
                    **_version_attribution_payload(item),
                    "change_comment": item.change_comment,
                }
                for item in page.versions
            ],
            "next_before_version": page.next_before_version,
        }

    @app.get(
        "/workspaces/{workspace_slug}/agents/{agent_slug}/memory/history/document"
    )
    async def read_document_history(
        workspace_slug: str,
        agent_slug: str,
        path: str = Query(min_length=1, max_length=4096),
        version: int = Query(ge=1),
        compare_to_version: int | None = Query(default=None, ge=1),
        principal: ManagementPrincipal = Depends(current_principal),
    ):
        invocation = await invocation_for(
            principal=principal,
            workspace_slug=workspace_slug,
            agent_slug=agent_slug,
            action=MemoryAction.HISTORY_READ,
        )
        item = await memory_service.read_history(
            invocation,
            path,
            version,
            compare_to_version=compare_to_version,
        )
        return {
            "path": item.path,
            "content": item.content,
            "version": item.version,
            "version_created_at": item.version_created_at.isoformat(),
            **_version_attribution_payload(item),
            "change_comment": item.change_comment,
            "compared_to_version": item.compared_to_version,
            "diff": item.diff,
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
            co_authored_by=body.co_authored_by,
            change_comment=body.change_comment,
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
            co_authored_by=body.co_authored_by,
            change_comment=body.change_comment,
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
    "DefaultWorkspace",
    "LocalManagementScope",
    "ManagementPrincipal",
    "create_management_api",
]
