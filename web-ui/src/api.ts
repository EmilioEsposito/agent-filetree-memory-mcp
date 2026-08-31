import type {
  AgentSummary,
  ContentRole,
  CurrentPrincipal,
  InvitationSummary,
  ManagementEvent,
  MemberAccess,
  MemoryDocument,
  MemoryEntry,
  WorkspaceSummary,
} from "./types";
import { apiUrl, getRuntimeConfig } from "./config";

export type TokenGetter = () => Promise<string | null>;

function detailFrom(payload: unknown): string {
  if (typeof payload === "string") return payload;
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          item && typeof item === "object" && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join("; ");
    }
  }
  return "The memory service could not complete the request.";
}

export async function memoryApi<T>(
  getIdToken: TokenGetter,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body != null) headers.set("Content-Type", "application/json");
  const token = await getIdToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const config = await getRuntimeConfig();
  const response = await fetch(apiUrl(config, path), {
    ...init,
    credentials: "same-origin",
    headers,
  });
  if (response.status === 204) return undefined as T;
  const payload = (await response.json().catch(() => null)) as unknown;
  if (!response.ok) throw new Error(detailFrom(payload));
  return payload as T;
}

function segment(value: string): string {
  return encodeURIComponent(value);
}

export function loadMe(getIdToken: TokenGetter) {
  return memoryApi<CurrentPrincipal>(getIdToken, "/me", {});
}

export async function loadWorkspaces(getIdToken: TokenGetter) {
  const result = await memoryApi<{ workspaces: WorkspaceSummary[] }>(
    getIdToken,
    "/workspaces",
    {}
  );
  return result.workspaces;
}

export function createWorkspace(
  getIdToken: TokenGetter,
  slug: string,
) {
  return memoryApi<WorkspaceSummary>(
    getIdToken,
    "/workspaces",
    { method: "POST", body: JSON.stringify({ slug }) }
  );
}

export async function loadAgents(
  getIdToken: TokenGetter,
  workspace: string,
) {
  const result = await memoryApi<{ agents: AgentSummary[] }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents`,
    {}
  );
  return result.agents;
}

export function createAgent(
  getIdToken: TokenGetter,
  workspace: string,
  slug: string,
  displayAlias: string,
) {
  return memoryApi<AgentSummary>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents`,
    {
      method: "POST",
      body: JSON.stringify({ slug, display_alias: displayAlias || null }),
    }
  );
}

export function updateAgentAlias(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  displayAlias: string,
) {
  return memoryApi<AgentSummary>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}`,
    {
      method: "PATCH",
      body: JSON.stringify({ display_alias: displayAlias }),
    }
  );
}

export async function loadMembers(
  getIdToken: TokenGetter,
  workspace: string,
) {
  return memoryApi<{ members: MemberAccess[]; invitations: InvitationSummary[] }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/members`,
    {}
  );
}

export function inviteMember(
  getIdToken: TokenGetter,
  workspace: string,
  email: string,
  role: "admin" | "member",
) {
  return memoryApi<{ created: "member" | "invitation" }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/members`,
    { method: "POST", body: JSON.stringify({ email, role }) }
  );
}

export function revokeInvitation(
  getIdToken: TokenGetter,
  workspace: string,
  invitationId: string,
) {
  return memoryApi<void>(
    getIdToken,
    `/workspaces/${segment(workspace)}/invitations/${segment(invitationId)}`,
    { method: "DELETE" }
  );
}

export function updateMemberRole(
  getIdToken: TokenGetter,
  workspace: string,
  targetPrincipalId: string,
  role: "admin" | "member",
) {
  return memoryApi<{ updated: boolean }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/members/role`,
    {
      method: "PUT",
      body: JSON.stringify({
        target_principal_id: targetPrincipalId,
        role,
      }),
    }
  );
}

export function transferWorkspaceOwnership(
  getIdToken: TokenGetter,
  workspace: string,
  targetPrincipalId: string,
) {
  return memoryApi<{ transferred: boolean }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/transfer-ownership`,
    {
      method: "POST",
      body: JSON.stringify({ target_principal_id: targetPrincipalId }),
    }
  );
}

export function removeMember(
  getIdToken: TokenGetter,
  workspace: string,
  targetPrincipalId: string,
) {
  return memoryApi<{ removed: boolean }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/members`,
    {
      method: "DELETE",
      body: JSON.stringify({ target_principal_id: targetPrincipalId }),
    }
  );
}

export async function loadAgentAccess(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
) {
  const result = await memoryApi<{ members: MemberAccess[] }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/access`,
    {}
  );
  return result.members;
}

export function setContentAccess(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  targetPrincipalId: string,
  contentRole: ContentRole | null,
) {
  return memoryApi<{ updated: boolean }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/content-access`,
    {
      method: "PUT",
      body: JSON.stringify({
        target_principal_id: targetPrincipalId,
        content_role: contentRole,
      }),
    }
  );
}

export function setAgentManager(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  targetPrincipalId: string,
  enabled: boolean,
) {
  return memoryApi<{ updated: boolean }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/manager`,
    {
      method: "PUT",
      body: JSON.stringify({ target_principal_id: targetPrincipalId, enabled }),
    }
  );
}

export async function loadAudit(
  getIdToken: TokenGetter,
  workspace: string,
) {
  const result = await memoryApi<{ events: ManagementEvent[] }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/audit`,
    {}
  );
  return result.events;
}

export async function loadMemoryEntries(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  path: string,
) {
  const result = await memoryApi<{ path: string; entries: MemoryEntry[] }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/memory?path=${encodeURIComponent(path)}`,
    {}
  );
  return result.entries;
}

export function loadMemoryDocument(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  path: string,
) {
  return memoryApi<MemoryDocument>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/memory/document?path=${encodeURIComponent(path)}`,
    {}
  );
}

export function saveMemoryDocument(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  path: string,
  content: string,
  expectedVersion: number | null,
) {
  return memoryApi<{ path: string; version: number; created: boolean }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/memory/document`,
    {
      method: "PUT",
      body: JSON.stringify({
        path,
        content,
        expected_version: expectedVersion,
        idempotency_key: crypto.randomUUID(),
      }),
    }
  );
}

export function deleteMemoryDocument(
  getIdToken: TokenGetter,
  workspace: string,
  agent: string,
  path: string,
  expectedVersion: number,
) {
  return memoryApi<{ path: string; deleted_version: number }>(
    getIdToken,
    `/workspaces/${segment(workspace)}/agents/${segment(agent)}/memory/document`,
    {
      method: "DELETE",
      body: JSON.stringify({
        path,
        expected_version: expectedVersion,
        idempotency_key: crypto.randomUUID(),
      }),
    }
  );
}
