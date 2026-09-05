export type WorkspaceRole = "owner" | "admin" | "member";
export type ContentRole = "reader" | "editor" | "full_access";
export type AgentAccessPolicy = "private" | "workspace_read";
export type WorkspaceAdmissionPolicy =
  | "invite_only"
  | "all_authenticated"
  | "external_entitlement";
export type WorkspaceAgentCreationPolicy = "admins_only" | "all_members";

export interface CurrentPrincipal {
  principal_id: string;
  email: string;
  display_name: string;
  allow_admin_self_grant: boolean;
  is_platform_admin: boolean;
  can_create_workspaces?: boolean;
  workspace_creation_restriction?: "policy" | "quota" | null;
  created_workspace_count?: number;
  workspace_creation_limit?: number;
}

export interface WorkspaceSummary {
  workspace_id: string;
  slug: string;
  role: WorkspaceRole | null;
  admission_policy: WorkspaceAdmissionPolicy;
  agent_creation_policy: WorkspaceAgentCreationPolicy;
  can_create_agents: boolean;
  agent_count: number;
  member_count: number;
  created_at: string;
}

export interface AgentSummary {
  agent_profile_id: string;
  slug: string;
  display_alias: string;
  content_role: ContentRole | null;
  explicit_content_role: ContentRole | null;
  access_policy: AgentAccessPolicy;
  can_manage: boolean;
  created_at: string;
}

export interface MemberAccess {
  principal_id: string;
  email: string | null;
  display_name: string | null;
  workspace_role: WorkspaceRole;
  content_role: ContentRole | null;
  effective_content_role: ContentRole | null;
  explicit_manager: boolean;
}

export interface InvitationSummary {
  invitation_id: string;
  email: string;
  role: "admin" | "member";
  invited_by_principal_id: string;
  created_at: string;
}

export interface ManagementEvent {
  event_id: string;
  actor_principal_id: string;
  action: string;
  target_kind: string;
  target_id: string;
  occurred_at: string;
}

export interface MemoryEntry {
  name: string;
  path: string;
  kind: "directory" | "document";
  version: number;
  updated_at: string;
}

export interface MemoryDocument {
  path: string;
  content: string;
  version: number;
  created_at: string;
  updated_at: string;
}
