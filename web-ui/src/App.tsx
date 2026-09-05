import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle,
  BookOpen,
  Bot,
  Check,
  ChevronRight,
  Copy,
  File,
  FilePlus2,
  Files,
  Folder,
  FolderOpen,
  History,
  KeyRound,
  Loader2,
  LockKeyhole,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Shield,
  Trash2,
  UserCog,
  UserPlus,
  Users,
  X,
} from "lucide-react";

import { AuthGuard, useAuth } from "./auth";
import {
  assignPlatformAdminRole,
  createAgent,
  createWorkspace,
  deleteMemoryDocument,
  inviteMember,
  joinWorkspace,
  loadAgentAccess,
  loadAgents,
  loadAudit,
  loadMe,
  loadMembers,
  loadMemoryDocument,
  loadMemoryEntries,
  loadWorkspaces,
  removeMember,
  revokeInvitation,
  saveMemoryDocument,
  setAgentAccessPolicy,
  setAgentManager,
  setContentAccess,
  transferAgentManagement,
  transferWorkspaceOwnership,
  updateAgentAlias,
  updateMemberRole,
  updateWorkspacePolicy,
} from "./api";
import { mcpConnectionUrl } from "./config";
import type {
  AgentAccessPolicy,
  AgentSummary,
  ContentRole,
  CurrentPrincipal,
  InvitationSummary,
  ManagementEvent,
  MemberAccess,
  MemoryDocument,
  MemoryEntry,
  WorkspaceAdmissionPolicy,
  WorkspaceAgentCreationPolicy,
  WorkspaceRole,
  WorkspaceSummary,
} from "./types";

const CONTAINER = "mx-auto w-full max-w-[1600px] px-4 sm:px-6 lg:px-8";

function useBrowserSearchParams(): [
  URLSearchParams,
  (next: URLSearchParams, options?: { replace?: boolean }) => void,
] {
  const [params, setParams] = useState(
    () => new URLSearchParams(window.location.search),
  );
  useEffect(() => {
    const refresh = () => setParams(new URLSearchParams(window.location.search));
    window.addEventListener("popstate", refresh);
    return () => window.removeEventListener("popstate", refresh);
  }, []);
  const update = useCallback(
    (next: URLSearchParams, options?: { replace?: boolean }) => {
      const query = next.toString();
      const url = window.location.pathname + (query ? `?${query}` : "");
      if (options?.replace === false) {
        window.history.pushState(null, "", url);
      } else {
        window.history.replaceState(null, "", url);
      }
      setParams(new URLSearchParams(next));
    },
    [],
  );
  return [params, update];
}

type Tab = "memory" | "access" | "members" | "audit";

interface SelfGrantRequest {
  member: MemberAccess;
  role: ContentRole;
}

const PANEL =
  "rounded-xl border border-gray-200 bg-white shadow-sm dark:border-gray-800 dark:bg-gray-900";
const INPUT =
  "w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-700 dark:bg-gray-950 dark:text-white";
const BUTTON =
  "inline-flex items-center justify-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50";

function displayDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function contentRoleLabel(role: ContentRole | null): string {
  if (role === "full_access") return "Full access";
  if (role === "editor") return "Editor";
  if (role === "reader") return "Reader";
  return "No content access";
}

function agentAccessPolicyLabel(policy: AgentAccessPolicy): string {
  return policy === "workspace_read" ? "Workspace read-only" : "Private";
}

function agentContentRoleLabel(agent: AgentSummary): string {
  if (
    agent.content_role === "reader" &&
    agent.explicit_content_role === null &&
    agent.access_policy === "workspace_read"
  ) {
    return "Workspace reader";
  }
  return contentRoleLabel(agent.content_role);
}

function workspaceRoleLabel(role: WorkspaceRole | null): string {
  if (role === null) return "Platform view";
  return role[0].toUpperCase() + role.slice(1);
}

function admissionPolicyLabel(policy: WorkspaceAdmissionPolicy): string {
  if (policy === "all_authenticated") return "All authenticated users";
  if (policy === "external_entitlement") return "External entitlement";
  return "Invite only";
}

function agentCreationPolicyLabel(
  policy: WorkspaceAgentCreationPolicy,
): string {
  return policy === "all_members" ? "All members" : "Administrators only";
}

function principalLabel(member: MemberAccess): string {
  return member.display_name || member.email || member.principal_id;
}

function normalizeNewPath(
  folder: string,
  filename: string,
): string {
  const trimmedFilename = filename.trim().replace(/^\/+|\/+$/g, "");
  if (!trimmedFilename) return "";
  const trimmedFolder = folder.trim().replace(/^\/+|\/+$/g, "");
  const relativePath = trimmedFolder
    ? `${trimmedFolder}/${trimmedFilename}`
    : trimmedFilename;
  return `/${relativePath}`;
}

function RoleBadge({ children, tone = "gray" }: { children: ReactNode; tone?: "blue" | "green" | "amber" | "gray" }) {
  const colors = {
    blue: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300",
    green: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300",
    amber: "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300",
    gray: "bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300",
  };
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${colors[tone]}`}>
      {children}
    </span>
  );
}
function EmptyState({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return (
    <div className="flex min-h-56 flex-col items-center justify-center px-6 py-10 text-center">
      <div className="mb-3 rounded-full bg-gray-100 p-3 text-gray-500 dark:bg-gray-800 dark:text-gray-400">
        {icon}
      </div>
      <h3 className="font-semibold text-gray-900 dark:text-white">{title}</h3>
      <p className="mt-1 max-w-md text-sm text-gray-500 dark:text-gray-400">{body}</p>
    </div>
  );
}

export default function AgentMemoryPage() {
  return (
    <AuthGuard>
      <AgentMemoryManager />
    </AuthGuard>
  );
}

function AgentMemoryManager() {
  const { config, getToken, logout } = useAuth();
  const [searchParams, setSearchParams] = useBrowserSearchParams();
  const [me, setMe] = useState<CurrentPrincipal | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [members, setMembers] = useState<MemberAccess[]>([]);
  const [invitations, setInvitations] = useState<InvitationSummary[]>([]);
  const [agentAccess, setAgentAccess] = useState<MemberAccess[]>([]);
  const [audit, setAudit] = useState<ManagementEvent[]>([]);
  const [selectedWorkspaceSlug, setSelectedWorkspaceSlug] = useState(
    () => searchParams.get("workspace") ?? "",
  );
  const [selectedAgentSlug, setSelectedAgentSlug] = useState(
    () => searchParams.get("agent") ?? "",
  );
  const [tab, setTab] = useState<Tab>(() => {
    const requested = searchParams.get("tab");
    return requested === "access" || requested === "members" || requested === "audit"
      ? requested
      : "memory";
  });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [workspaceSlugInput, setWorkspaceSlugInput] = useState("");
  const [newWorkspaceAdmission, setNewWorkspaceAdmission] =
    useState<WorkspaceAdmissionPolicy>("invite_only");
  const [newWorkspaceAgentCreation, setNewWorkspaceAgentCreation] =
    useState<WorkspaceAgentCreationPolicy>("admins_only");
  const [policyAdmissionDraft, setPolicyAdmissionDraft] =
    useState<WorkspaceAdmissionPolicy>("invite_only");
  const [policyAgentCreationDraft, setPolicyAgentCreationDraft] =
    useState<WorkspaceAgentCreationPolicy>("admins_only");
  const [agentSlugInput, setAgentSlugInput] = useState("");
  const [agentAliasInput, setAgentAliasInput] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"admin" | "member">("member");
  const [editingAlias, setEditingAlias] = useState(false);
  const [aliasDraft, setAliasDraft] = useState("");
  const [selfGrant, setSelfGrant] = useState<SelfGrantRequest | null>(null);
  const [mcpUrlCopied, setMcpUrlCopied] = useState(false);

  const [directory, setDirectory] = useState("/");
  const [entries, setEntries] = useState<MemoryEntry[]>([]);
  const [document, setDocument] = useState<MemoryDocument | null>(null);
  const [editorContent, setEditorContent] = useState("");
  const [editorPath, setEditorPath] = useState("");
  const [creatingDocument, setCreatingDocument] = useState(false);
  const [addFileOpen, setAddFileOpen] = useState(false);
  const [newDocumentFolder, setNewDocumentFolder] = useState("");
  const [newDocumentName, setNewDocumentName] = useState("");
  const [editingDocument, setEditingDocument] = useState(false);

  const selectedWorkspace = useMemo(
    () => workspaces.find((item) => item.slug === selectedWorkspaceSlug) ?? null,
    [selectedWorkspaceSlug, workspaces],
  );
  const selectedAgent = useMemo(
    () => agents.find((item) => item.slug === selectedAgentSlug) ?? null,
    [agents, selectedAgentSlug],
  );
  const selectedMcpUrl = useMemo(
    () =>
      config && selectedWorkspaceSlug && selectedAgent
        ? mcpConnectionUrl(
            config,
            selectedWorkspaceSlug,
            selectedAgent.slug,
          ).toString()
        : "",
    [config, selectedAgent, selectedWorkspaceSlug],
  );
  const workspaceAdmin = selectedWorkspace?.role === "owner" || selectedWorkspace?.role === "admin";
  const workspaceOwner = selectedWorkspace?.role === "owner";
  const canCreateAgent = selectedWorkspace?.can_create_agents ?? false;
  const canWrite = selectedAgent?.content_role === "editor" || selectedAgent?.content_role === "full_access";
  const canDelete = selectedAgent?.content_role === "full_access";
  const editorDirty = document
    ? editorContent !== document.content
    : creatingDocument && (editorContent.length > 0 || editorPath.length > 0);

  useEffect(() => {
    setMcpUrlCopied(false);
  }, [selectedMcpUrl]);

  useEffect(() => {
    if (!selectedWorkspace) return;
    setPolicyAdmissionDraft(selectedWorkspace.admission_policy);
    setPolicyAgentCreationDraft(selectedWorkspace.agent_creation_policy);
  }, [selectedWorkspace]);

  const updateUrl = useCallback(
    (workspace: string, agent: string, nextTab: Tab) => {
      const next = new URLSearchParams();
      if (workspace) next.set("workspace", workspace);
      if (agent) next.set("agent", agent);
      if (nextTab !== "memory") next.set("tab", nextTab);
      setSearchParams(next, { replace: true });
    },
    [setSearchParams],
  );

  const fail = useCallback((caught: unknown) => {
    setError(caught instanceof Error ? caught.message : String(caught));
  }, []);

  const runMutation = useCallback(
    async (name: string, operation: () => Promise<void>, success: string) => {
      setBusy(name);
      setError("");
      setNotice("");
      try {
        await operation();
        setNotice(success);
      } catch (caught) {
        fail(caught);
        throw caught;
      } finally {
        setBusy("");
      }
    },
    [fail],
  );

  const refreshWorkspaces = useCallback(async () => {
    const next = await loadWorkspaces(getToken);
    setWorkspaces(next);
    return next;
  }, [getToken]);

  const refreshAgents = useCallback(
    async (workspaceSlug: string) => {
      const next = await loadAgents(getToken, workspaceSlug);
      setAgents(next);
      return next;
    },
    [getToken],
  );

  const refreshMembers = useCallback(
    async (workspaceSlug: string) => {
      const next = await loadMembers(getToken, workspaceSlug);
      setMembers(next.members);
      setInvitations(next.invitations);
    },
    [getToken],
  );

  const refreshAgentAccess = useCallback(
    async (workspaceSlug: string, agentSlug: string) => {
      setAgentAccess(await loadAgentAccess(getToken, workspaceSlug, agentSlug));
    },
    [getToken],
  );

  const refreshAudit = useCallback(
    async (workspaceSlug: string) => {
      setAudit(await loadAudit(getToken, workspaceSlug));
    },
    [getToken],
  );

  const openDirectory = useCallback(
    async (workspaceSlug: string, agentSlug: string, path: string) => {
      setBusy("memory-load");
      setError("");
      try {
        setEntries(await loadMemoryEntries(getToken, workspaceSlug, agentSlug, path));
        setDirectory(path);
        setDocument(null);
        setEditorContent("");
        setEditorPath("");
        setCreatingDocument(false);
        setEditingDocument(false);
        setAddFileOpen(false);
      } catch (caught) {
        fail(caught);
      } finally {
        setBusy("");
      }
    },
    [fail, getToken],
  );

  useEffect(() => {
    let cancelled = false;
    async function initialize() {
      setLoading(true);
      setError("");
      try {
        const [principal, nextWorkspaces] = await Promise.all([
          loadMe(getToken),
          loadWorkspaces(getToken),
        ]);
        if (cancelled) return;
        setMe(principal);
        setWorkspaces(nextWorkspaces);
        const requested = searchParams.get("workspace");
        const chosen = nextWorkspaces.some((item) => item.slug === requested)
          ? requested!
          : nextWorkspaces[0]?.slug ?? "";
        setSelectedWorkspaceSlug(chosen);
      } catch (caught) {
        if (!cancelled) fail(caught);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void initialize();
    return () => {
      cancelled = true;
    };
    // Initial URL parameters are intentionally consumed only once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [getToken]);

  useEffect(() => {
    if (!selectedWorkspaceSlug) {
      setAgents([]);
      setSelectedAgentSlug("");
      return;
    }
    let cancelled = false;
    setBusy("workspace-load");
    setError("");
    async function loadWorkspace() {
      try {
        const currentWorkspace = workspaces.find(
          (item) => item.slug === selectedWorkspaceSlug,
        );
        // The deep link seeds selectedWorkspaceSlug before the asynchronous
        // workspace catalog arrives. Do not clear its requested agent while
        // that catalog is still empty; initialization will replace an invalid
        // workspace with the first accessible one after loading completes.
        if (!currentWorkspace) return;
        if (!currentWorkspace?.role) {
          setAgents([]);
          setSelectedAgentSlug("");
          setMembers([]);
          setInvitations([]);
          setAudit([]);
          return;
        }
        const nextAgents = await loadAgents(getToken, selectedWorkspaceSlug);
        if (cancelled) return;
        setAgents(nextAgents);
        const requested = searchParams.get("agent");
        const chosen = nextAgents.some((item) => item.slug === requested)
          ? requested!
          : nextAgents[0]?.slug ?? "";
        setSelectedAgentSlug(chosen);
        if (currentWorkspace?.role === "owner" || currentWorkspace?.role === "admin") {
          await Promise.all([
            refreshMembers(selectedWorkspaceSlug),
            refreshAudit(selectedWorkspaceSlug),
          ]);
        } else {
          setMembers([]);
          setInvitations([]);
          setAudit([]);
        }
      } catch (caught) {
        if (!cancelled) fail(caught);
      } finally {
        if (!cancelled) setBusy("");
      }
    }
    void loadWorkspace();
    return () => {
      cancelled = true;
    };
  }, [fail, getToken, refreshAudit, refreshMembers, selectedWorkspaceSlug, workspaces]);

  useEffect(() => {
    if (!selectedWorkspaceSlug || !selectedAgentSlug) {
      setAgentAccess([]);
      setEntries([]);
      return;
    }
    const agent = agents.find((item) => item.slug === selectedAgentSlug);
    if (!agent) return;
    const currentAgent = agent;
    let cancelled = false;
    async function loadAgent() {
      try {
        if (currentAgent.can_manage) {
          const nextAccess = await loadAgentAccess(
            getToken,
            selectedWorkspaceSlug,
            selectedAgentSlug,
          );
          if (!cancelled) setAgentAccess(nextAccess);
        } else if (!cancelled) {
          setAgentAccess([]);
        }
        if (currentAgent.content_role) {
          const nextEntries = await loadMemoryEntries(
            getToken,
            selectedWorkspaceSlug,
            selectedAgentSlug,
            "/",
          );
          if (!cancelled) {
            setDirectory("/");
            setEntries(nextEntries);
          }
        } else if (!cancelled) {
          setEntries([]);
          setDocument(null);
        }
      } catch (caught) {
        if (!cancelled) fail(caught);
      }
    }
    void loadAgent();
    return () => {
      cancelled = true;
    };
  }, [agents, fail, getToken, selectedAgentSlug, selectedWorkspaceSlug]);

  useEffect(() => {
    updateUrl(selectedWorkspaceSlug, selectedAgentSlug, tab);
  }, [selectedAgentSlug, selectedWorkspaceSlug, tab, updateUrl]);

  useEffect(() => {
    if (!selectedAgent) return;
    if (tab === "access" && !selectedAgent.can_manage) setTab("memory");
    if ((tab === "members" || tab === "audit") && !workspaceAdmin) setTab("memory");
  }, [selectedAgent, tab, workspaceAdmin]);

  async function handleCreateWorkspace() {
    const slug = workspaceSlugInput.trim();
    if (!slug) return;
    await runMutation(
      "workspace-create",
      async () => {
        await createWorkspace(
          getToken,
          slug,
          newWorkspaceAdmission,
          newWorkspaceAgentCreation,
        );
        await refreshWorkspaces();
        setWorkspaceSlugInput("");
        setSelectedWorkspaceSlug(slug);
        setSelectedAgentSlug("");
      },
      `Workspace ${slug} created.`,
    ).catch(() => undefined);
  }

  async function handleCreateAgent() {
    if (!selectedWorkspaceSlug) return;
    const slug = agentSlugInput.trim();
    if (!slug) return;
    await runMutation(
      "agent-create",
      async () => {
        await createAgent(
          getToken,
          selectedWorkspaceSlug,
          slug,
          agentAliasInput.trim(),
        );
        await Promise.all([
          refreshAgents(selectedWorkspaceSlug),
          refreshWorkspaces(),
          workspaceAdmin
            ? refreshAudit(selectedWorkspaceSlug)
            : Promise.resolve(),
        ]);
        setAgentSlugInput("");
        setAgentAliasInput("");
        setSelectedAgentSlug(slug);
      },
      `Agent ${slug} created with full access and explicit management authority.`,
    ).catch(() => undefined);
  }

  async function handleAliasSave() {
    if (!selectedWorkspaceSlug || !selectedAgentSlug || !aliasDraft.trim()) return;
    await runMutation(
      "alias-save",
      async () => {
        await updateAgentAlias(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          aliasDraft.trim(),
        );
        await refreshAgents(selectedWorkspaceSlug);
        setEditingAlias(false);
      },
      "Agent display name updated.",
    ).catch(() => undefined);
  }

  async function handleAgentAccessPolicy(next: AgentAccessPolicy) {
    if (!selectedWorkspaceSlug || !selectedAgentSlug || !selectedAgent) return;
    if (
      next === "workspace_read" &&
      !window.confirm(
        `Share ${selectedAgent.display_alias} read-only with every current and future member of ${selectedWorkspaceSlug}? Editing and deletion will still require an explicit individual role.`,
      )
    ) {
      return;
    }
    await runMutation(
      "agent-access-policy",
      async () => {
        await setAgentAccessPolicy(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          next,
          next === "workspace_read",
        );
        await Promise.all([
          refreshAgents(selectedWorkspaceSlug),
          refreshAgentAccess(selectedWorkspaceSlug, selectedAgentSlug),
          workspaceAdmin
            ? refreshAudit(selectedWorkspaceSlug)
            : Promise.resolve(),
        ]);
      },
      next === "workspace_read"
        ? "Everyone in this workspace can now read this memory store."
        : "This memory store is private again; explicit individual access is unchanged.",
    ).catch(() => undefined);
  }

  async function handleInvite() {
    if (!selectedWorkspaceSlug || !inviteEmail.trim()) return;
    await runMutation(
      "member-invite",
      async () => {
        await inviteMember(
          getToken,
          selectedWorkspaceSlug,
          inviteEmail.trim(),
          inviteRole,
        );
        await Promise.all([
          refreshMembers(selectedWorkspaceSlug),
          refreshWorkspaces(),
          refreshAudit(selectedWorkspaceSlug),
        ]);
        setInviteEmail("");
      },
      "Workspace invitation recorded.",
    ).catch(() => undefined);
  }

  async function applyContentRole(member: MemberAccess, role: ContentRole | null) {
    if (!selectedWorkspaceSlug || !selectedAgentSlug) return;
    if (member.principal_id === me?.principal_id && role !== null) {
      if (!workspaceAdmin || !me.allow_admin_self_grant) return;
      setSelfGrant({ member, role });
      return;
    }
    await runMutation(
      `content-${member.principal_id}`,
      async () => {
        await setContentAccess(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          member.principal_id,
          role,
        );
        await Promise.all([
          refreshAgentAccess(selectedWorkspaceSlug, selectedAgentSlug),
          refreshAgents(selectedWorkspaceSlug),
          workspaceAdmin ? refreshAudit(selectedWorkspaceSlug) : Promise.resolve(),
        ]);
      },
      role ? `${principalLabel(member)} now has ${contentRoleLabel(role).toLowerCase()}.` : `${principalLabel(member)} no longer has content access.`,
    ).catch(() => undefined);
  }

  async function confirmSelfGrant() {
    if (!selfGrant || !selectedWorkspaceSlug || !selectedAgentSlug) return;
    const request = selfGrant;
    setSelfGrant(null);
    await runMutation(
      "self-grant",
      async () => {
        await setContentAccess(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          request.member.principal_id,
          request.role,
          true,
        );
        await Promise.all([
          refreshAgentAccess(selectedWorkspaceSlug, selectedAgentSlug),
          refreshAgents(selectedWorkspaceSlug),
          refreshAudit(selectedWorkspaceSlug),
        ]);
      },
      `You now have ${contentRoleLabel(request.role).toLowerCase()}.`,
    ).catch(() => undefined);
  }

  async function handleManagerChange(member: MemberAccess, next: boolean) {
    if (!selectedWorkspaceSlug || !selectedAgentSlug) return;
    await runMutation(
      `manager-${member.principal_id}`,
      async () => {
        await setAgentManager(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          member.principal_id,
          next,
        );
        await Promise.all([
          refreshAgentAccess(selectedWorkspaceSlug, selectedAgentSlug),
          refreshAudit(selectedWorkspaceSlug),
        ]);
      },
      next ? `${principalLabel(member)} can now manage this agent.` : `${principalLabel(member)} no longer manages this agent.`,
    ).catch(() => undefined);
  }

  async function handlePlatformAdminRole() {
    if (!selectedWorkspaceSlug || !me?.is_platform_admin) return;
    await runMutation(
      "platform-admin-role",
      async () => {
        await assignPlatformAdminRole(getToken, selectedWorkspaceSlug);
        await refreshWorkspaces();
      },
      "Workspace administrator role assigned. Memory content access was not granted.",
    ).catch(() => undefined);
  }

  async function handleJoinWorkspace() {
    if (!selectedWorkspaceSlug) return;
    await runMutation(
      "workspace-join",
      async () => {
        await joinWorkspace(getToken, selectedWorkspaceSlug);
        await refreshWorkspaces();
      },
      `You joined ${selectedWorkspaceSlug} as a member.`,
    ).catch(() => undefined);
  }

  async function handlePolicySave() {
    if (!selectedWorkspaceSlug || !workspaceAdmin) return;
    await runMutation(
      "workspace-policy",
      async () => {
        await updateWorkspacePolicy(
          getToken,
          selectedWorkspaceSlug,
          policyAdmissionDraft,
          policyAgentCreationDraft,
        );
        await Promise.all([
          refreshWorkspaces(),
          refreshAudit(selectedWorkspaceSlug),
        ]);
      },
      "Workspace authorization policy updated.",
    ).catch(() => undefined);
  }

  async function handleManagerTransfer(member: MemberAccess) {
    if (!selectedWorkspaceSlug || !selectedAgentSlug) return;
    if (
      !window.confirm(
        `Transfer explicit management of this agent to ${principalLabel(member)}?`,
      )
    ) {
      return;
    }
    await runMutation(
      `manager-transfer-${member.principal_id}`,
      async () => {
        await transferAgentManagement(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          member.principal_id,
        );
        await Promise.all([
          refreshAgents(selectedWorkspaceSlug),
          workspaceAdmin
            ? Promise.all([
                refreshAgentAccess(selectedWorkspaceSlug, selectedAgentSlug),
                refreshAudit(selectedWorkspaceSlug),
              ])
            : Promise.resolve(),
        ]);
        if (!workspaceAdmin) {
          setAgentAccess([]);
          setTab("memory");
        }
      },
      `Agent management transferred to ${principalLabel(member)}.`,
    ).catch(() => undefined);
  }

  async function openDocument(path: string) {
    if (!selectedWorkspaceSlug || !selectedAgentSlug) return;
    setBusy("document-load");
    setError("");
    try {
      const next = await loadMemoryDocument(
        getToken,
        selectedWorkspaceSlug,
        selectedAgentSlug,
        path,
      );
      setDocument(next);
      setEditorPath(next.path);
      setEditorContent(next.content);
      setCreatingDocument(false);
      setEditingDocument(false);
      setAddFileOpen(false);
    } catch (caught) {
      fail(caught);
    } finally {
      setBusy("");
    }
  }

  async function saveDocument() {
    if (!selectedWorkspaceSlug || !selectedAgentSlug || !editorPath) return;
    const expectedVersion = document?.version ?? null;
    await runMutation(
      "document-save",
      async () => {
        await saveMemoryDocument(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          editorPath,
          editorContent,
          expectedVersion,
        );
        const saved = await loadMemoryDocument(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          editorPath,
        );
        setDocument(saved);
        setEditorContent(saved.content);
        setCreatingDocument(false);
        setEditingDocument(false);
        setEntries(
          await loadMemoryEntries(
            getToken,
            selectedWorkspaceSlug,
            selectedAgentSlug,
            directory,
          ),
        );
      },
      `Saved ${editorPath}.`,
    ).catch(() => undefined);
  }

  async function deleteDocument() {
    if (!selectedWorkspaceSlug || !selectedAgentSlug || !document) return;
    if (
      !window.confirm(
        `Delete ${document.path} at version ${document.version}? It will become unreadable immediately.`,
      )
    ) return;
    await runMutation(
      "document-delete",
      async () => {
        await deleteMemoryDocument(
          getToken,
          selectedWorkspaceSlug,
          selectedAgentSlug,
          document.path,
          document.version,
        );
        setDocument(null);
        setEditorPath("");
        setEditorContent("");
        setEntries(
          await loadMemoryEntries(
            getToken,
            selectedWorkspaceSlug,
            selectedAgentSlug,
            directory,
          ),
        );
      },
      `${document.path} deleted.`,
    ).catch(() => undefined);
  }

  function beginNewDocument() {
    const path = normalizeNewPath(
      newDocumentFolder,
      newDocumentName,
    );
    if (!path) return;
    setDocument(null);
    setEditorPath(path);
    setEditorContent("");
    setCreatingDocument(true);
    setEditingDocument(true);
    setAddFileOpen(false);
    setNewDocumentFolder("");
    setNewDocumentName("");
  }

  function cancelDocumentEdit() {
    if (creatingDocument) {
      setCreatingDocument(false);
      setEditorContent("");
      setEditorPath("");
    } else if (document) {
      setEditorContent(document.content);
    }
    setEditingDocument(false);
  }

  async function copySelectedMcpUrl() {
    if (!selectedMcpUrl) return;
    setError("");
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard access is unavailable in this browser.");
      }
      await navigator.clipboard.writeText(selectedMcpUrl);
      setMcpUrlCopied(true);
    } catch (caught) {
      fail(caught);
    }
  }

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-950">
        <Loader2 className="h-10 w-10 animate-spin text-blue-600" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 text-gray-900 dark:bg-gray-950 dark:text-gray-100">
      <header className="border-b border-gray-200 bg-white dark:border-gray-800 dark:bg-gray-900">
        <div className={`${CONTAINER} flex flex-col gap-4 py-6 sm:flex-row sm:items-center sm:justify-between`}>
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-blue-100 p-3 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300">
              <Files className="h-7 w-7" />
            </div>
            <div>
              <h1 className="text-2xl font-bold">Agent memory</h1>
              <p className="text-sm text-gray-500 dark:text-gray-400">
                Manage namespaces and access separately from encrypted content.
              </p>
            </div>
          </div>
          {me && (
            <div className="text-left text-sm sm:text-right">
              <p className="flex items-center gap-2 font-medium sm:justify-end">
                {me.display_name}
                {me.is_platform_admin && <RoleBadge tone="blue">Platform admin</RoleBadge>}
              </p>
              <p className="text-gray-500 dark:text-gray-400">{me.email}</p>
              {config?.auth.mode === "oidc" && (
                <button className="mt-2 text-blue-600 hover:underline dark:text-blue-400"
                  onClick={() => void logout()}>
                  Sign out
                </button>
              )}
            </div>
          )}
        </div>
      </header>

      <main className={`${CONTAINER} py-6`}>
        {error && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
            <span className="flex-1">{error}</span>
            <button aria-label="Dismiss error" onClick={() => setError("")}><X className="h-4 w-4" /></button>
          </div>
        )}
        {notice && (
          <div className="mb-4 flex items-start gap-3 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-200">
            <Check className="mt-0.5 h-5 w-5 shrink-0" />
            <span className="flex-1">{notice}</span>
            <button aria-label="Dismiss notice" onClick={() => setNotice("")}><X className="h-4 w-4" /></button>
          </div>
        )}

        <div className="grid gap-5 lg:grid-cols-[260px_280px_minmax(0,1fr)]">
          <aside className={`${PANEL} h-fit overflow-hidden`}>
            <div className="border-b border-gray-200 px-4 py-3 dark:border-gray-800">
              <div className="flex items-center gap-2 font-semibold"><Users className="h-4 w-4" /> Workspaces</div>
            </div>
            <div className="max-h-80 overflow-y-auto p-2">
              {workspaces.map((workspace) => (
                <button
                  key={workspace.workspace_id}
                  onClick={() => {
                    setSelectedWorkspaceSlug(workspace.slug);
                    setSelectedAgentSlug("");
                    setTab("memory");
                  }}
                  className={`mb-1 w-full rounded-lg px-3 py-2 text-left transition ${selectedWorkspaceSlug === workspace.slug ? "bg-blue-50 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200" : "hover:bg-gray-100 dark:hover:bg-gray-800"}`}
                >
                  <div className="truncate text-sm font-medium">{workspace.slug}</div>
                  <div className="mt-1 flex items-center justify-between text-xs text-gray-500 dark:text-gray-400">
                    <span>{workspace.agent_count} agents</span>
                    <span>{workspaceRoleLabel(workspace.role)}</span>
                  </div>
                </button>
              ))}
              {workspaces.length === 0 && <p className="p-3 text-sm text-gray-500">No workspaces yet.</p>}
            </div>
            {selectedWorkspace && (
              <div className="space-y-3 border-t border-gray-200 p-3 text-xs dark:border-gray-800">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-gray-500">Your role</span>
                  <RoleBadge tone={selectedWorkspace.role ? "blue" : "gray"}>{workspaceRoleLabel(selectedWorkspace.role)}</RoleBadge>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <span className="text-gray-500">Admission</span>
                  <span className="text-right font-medium">{admissionPolicyLabel(selectedWorkspace.admission_policy)}</span>
                </div>
                <div className="flex items-start justify-between gap-2">
                  <span className="text-gray-500">Agent creation</span>
                  <span className="text-right font-medium">{agentCreationPolicyLabel(selectedWorkspace.agent_creation_policy)}</span>
                </div>
                {!selectedWorkspace.role && (
                  <div className="space-y-2 border-t border-gray-200 pt-3 dark:border-gray-800">
                    {me?.is_platform_admin && (
                      <button className={`${BUTTON} w-full bg-blue-600 text-white hover:bg-blue-700`} disabled={busy === "platform-admin-role"} onClick={() => void handlePlatformAdminRole()}>
                        {busy === "platform-admin-role" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Shield className="h-4 w-4" />} Join as administrator
                      </button>
                    )}
                    {selectedWorkspace.admission_policy !== "invite_only" && (
                      <button className={`${BUTTON} w-full bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200`} disabled={busy === "workspace-join"} onClick={() => void handleJoinWorkspace()}>
                        {busy === "workspace-join" ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />} Join as member
                      </button>
                    )}
                  </div>
                )}
                {workspaceAdmin && (
                  <div className="space-y-2 border-t border-gray-200 pt-3 dark:border-gray-800">
                    <label className="block font-medium text-gray-500">Admission policy</label>
                    <select className={INPUT} value={policyAdmissionDraft} onChange={(event) => setPolicyAdmissionDraft(event.target.value as WorkspaceAdmissionPolicy)}>
                      <option value="invite_only">Invite only</option>
                      <option value="all_authenticated">All authenticated users</option>
                      <option value="external_entitlement">External entitlement</option>
                    </select>
                    <label className="block font-medium text-gray-500">Who may create agents</label>
                    <select className={INPUT} value={policyAgentCreationDraft} onChange={(event) => setPolicyAgentCreationDraft(event.target.value as WorkspaceAgentCreationPolicy)}>
                      <option value="admins_only">Administrators only</option>
                      <option value="all_members">All members</option>
                    </select>
                    <button className={`${BUTTON} w-full bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200`} disabled={busy === "workspace-policy"} onClick={() => void handlePolicySave()}>
                      {busy === "workspace-policy" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} Save policies
                    </button>
                  </div>
                )}
              </div>
            )}
            {me?.is_platform_admin && (
              <div className="space-y-2 border-t border-gray-200 p-3 dark:border-gray-800">
                <label className="block text-xs font-medium text-gray-500">Create workspace</label>
                <input className={INPUT} value={workspaceSlugInput} onChange={(event) => setWorkspaceSlugInput(event.target.value)} placeholder="workspace-slug" />
                <select aria-label="New workspace admission policy" className={INPUT} value={newWorkspaceAdmission} onChange={(event) => setNewWorkspaceAdmission(event.target.value as WorkspaceAdmissionPolicy)}>
                  <option value="invite_only">Invite only</option>
                  <option value="all_authenticated">All authenticated users</option>
                  <option value="external_entitlement">External entitlement</option>
                </select>
                <select aria-label="New workspace agent creation policy" className={INPUT} value={newWorkspaceAgentCreation} onChange={(event) => setNewWorkspaceAgentCreation(event.target.value as WorkspaceAgentCreationPolicy)}>
                  <option value="admins_only">Administrators create agents</option>
                  <option value="all_members">All members create agents</option>
                </select>
                <button aria-label="Create workspace" className={`${BUTTON} w-full bg-blue-600 text-white hover:bg-blue-700`} disabled={!workspaceSlugInput.trim() || busy === "workspace-create"} onClick={() => void handleCreateWorkspace()}>
                  {busy === "workspace-create" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} Create workspace
                </button>
              </div>
            )}
          </aside>

          <aside className={`${PANEL} h-fit overflow-hidden`}>
            <div className="border-b border-gray-200 px-4 py-3 dark:border-gray-800">
              <div className="flex items-center gap-2 font-semibold"><Bot className="h-4 w-4" /> Agents</div>
            </div>
            <div className="max-h-96 overflow-y-auto p-2">
              {agents.map((agent) => (
                <button
                  key={agent.agent_profile_id}
                  onClick={() => {
                    if (editorDirty && !window.confirm("Discard unsaved memory edits?")) return;
                    setSelectedAgentSlug(agent.slug);
                    setTab("memory");
                    setDocument(null);
                  }}
                  className={`mb-1 w-full rounded-lg px-3 py-2 text-left transition ${selectedAgentSlug === agent.slug ? "bg-blue-50 text-blue-800 dark:bg-blue-950/50 dark:text-blue-200" : "hover:bg-gray-100 dark:hover:bg-gray-800"}`}
                >
                  <div className="truncate text-sm font-medium">{agent.display_alias}</div>
                  <div className="truncate text-xs text-gray-500 dark:text-gray-400">{agent.slug}</div>
                  <div className="mt-2 flex flex-wrap gap-1">
                    {agent.can_manage && <RoleBadge tone="blue">Manage</RoleBadge>}
                    <RoleBadge tone={agent.access_policy === "workspace_read" ? "blue" : "gray"}>{agentAccessPolicyLabel(agent.access_policy)}</RoleBadge>
                    <RoleBadge tone={agent.content_role ? "green" : "gray"}>{agentContentRoleLabel(agent)}</RoleBadge>
                  </div>
                </button>
              ))}
              {selectedWorkspaceSlug && agents.length === 0 && <p className="p-3 text-sm text-gray-500">No visible agents.</p>}
              {!selectedWorkspaceSlug && <p className="p-3 text-sm text-gray-500">Select a workspace.</p>}
            </div>
            {canCreateAgent && (
              <div className="space-y-2 border-t border-gray-200 p-3 dark:border-gray-800">
                <label className="block text-xs font-medium text-gray-500">Create agent</label>
                <input className={INPUT} value={agentSlugInput} onChange={(event) => setAgentSlugInput(event.target.value)} placeholder="agent-slug" />
                <input className={INPUT} value={agentAliasInput} onChange={(event) => setAgentAliasInput(event.target.value)} placeholder="Display name (optional)" />
                <button className={`${BUTTON} w-full bg-blue-600 text-white hover:bg-blue-700`} disabled={!agentSlugInput.trim() || busy === "agent-create"} onClick={() => void handleCreateAgent()}>
                  {busy === "agent-create" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} Create agent
                </button>
                <p className="text-xs text-gray-500">You will receive explicit management authority and full memory access.</p>
              </div>
            )}
          </aside>

          <section className={`${PANEL} min-h-[640px] overflow-hidden`}>
            {!selectedAgent ? (
              <EmptyState
                icon={selectedWorkspace && !selectedWorkspace.role ? <LockKeyhole className="h-7 w-7" /> : <Bot className="h-7 w-7" />}
                title={selectedWorkspace && !selectedWorkspace.role ? "Join this workspace to see agents" : "Select an agent"}
                body={selectedWorkspace && !selectedWorkspace.role ? "Platform-wide inventory includes workspace metadata only. Agent names remain hidden until you explicitly join or assign yourself a workspace role." : "Choose an agent namespace, or create one when workspace policy allows it."}
              />
            ) : (
              <>
                <div className="border-b border-gray-200 px-5 py-4 dark:border-gray-800">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      {editingAlias ? (
                        <div className="flex gap-2">
                          <input className={INPUT} value={aliasDraft} onChange={(event) => setAliasDraft(event.target.value)} />
                          <button className={`${BUTTON} bg-blue-600 text-white`} onClick={() => void handleAliasSave()}><Save className="h-4 w-4" /></button>
                          <button className={`${BUTTON} bg-gray-100 dark:bg-gray-800`} onClick={() => setEditingAlias(false)}><X className="h-4 w-4" /></button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-2">
                          <h2 className="text-xl font-bold">{selectedAgent.display_alias}</h2>
                          {selectedAgent.can_manage && (
                            <button aria-label="Edit agent display name" className="text-gray-400 hover:text-blue-600" onClick={() => { setAliasDraft(selectedAgent.display_alias); setEditingAlias(true); }}><Pencil className="h-4 w-4" /></button>
                          )}
                        </div>
                      )}
                      <p className="mt-1 font-mono text-xs text-gray-500">/{selectedWorkspaceSlug}/{selectedAgent.slug}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {selectedAgent.can_manage && <RoleBadge tone="blue">Can manage</RoleBadge>}
                      <RoleBadge tone={selectedAgent.access_policy === "workspace_read" ? "blue" : "gray"}>{agentAccessPolicyLabel(selectedAgent.access_policy)}</RoleBadge>
                      <RoleBadge tone={selectedAgent.content_role ? "green" : "gray"}>{agentContentRoleLabel(selectedAgent)}</RoleBadge>
                    </div>
                  </div>
                  {selectedMcpUrl && (
                    <div className="mt-4">
                      <label className="block text-xs font-medium text-gray-500 dark:text-gray-400">
                        MCP connection URL
                      </label>
                      <div className="mt-1 flex flex-col gap-2 sm:flex-row">
                        <input
                          aria-label="MCP connection URL"
                          className={`${INPUT} font-mono text-xs`}
                          onFocus={(event) => event.currentTarget.select()}
                          readOnly
                          value={selectedMcpUrl}
                        />
                        <button
                          aria-label="Copy MCP connection URL"
                          className={`${BUTTON} shrink-0 bg-blue-600 text-white hover:bg-blue-700`}
                          onClick={() => void copySelectedMcpUrl()}
                          title="Copy MCP connection URL"
                        >
                          {mcpUrlCopied ? (
                            <Check className="h-4 w-4" />
                          ) : (
                            <Copy className="h-4 w-4" />
                          )}
                          {mcpUrlCopied ? "Copied" : "Copy"}
                        </button>
                      </div>
                    </div>
                  )}
                </div>

                <nav className="flex gap-1 overflow-x-auto border-b border-gray-200 px-4 pt-2 dark:border-gray-800">
                  {([
                    ["memory", "Memory", FolderOpen],
                    ...(selectedAgent.can_manage ? [["access", "Access", KeyRound] as const] : []),
                    ...(workspaceAdmin ? [["members", "Members", Users] as const, ["audit", "Audit", History] as const] : []),
                  ] as const).map(([value, label, Icon]) => (
                    <button key={value} onClick={() => setTab(value)} className={`flex items-center gap-2 border-b-2 px-3 py-2 text-sm font-medium ${tab === value ? "border-blue-600 text-blue-700 dark:text-blue-300" : "border-transparent text-gray-500 hover:text-gray-900 dark:hover:text-white"}`}>
                      <Icon className="h-4 w-4" /> {label}
                    </button>
                  ))}
                </nav>

                {tab === "memory" && (
                  <MemoryPanel
                    agent={selectedAgent}
                    directory={directory}
                    entries={entries}
                    document={document}
                    editorContent={editorContent}
                    editorPath={editorPath}
                    creatingDocument={creatingDocument}
                    addFileOpen={addFileOpen}
                    newDocumentFolder={newDocumentFolder}
                    newDocumentName={newDocumentName}
                    editingDocument={editingDocument}
                    editorDirty={editorDirty}
                    busy={busy}
                    canWrite={canWrite}
                    canDelete={canDelete}
                    selfGrantAllowed={Boolean(workspaceAdmin && me?.allow_admin_self_grant)}
                    currentMember={agentAccess.find((item) => item.principal_id === me?.principal_id) ?? null}
                    onSelfGrant={(member) => setSelfGrant({ member, role: "reader" })}
                    onDirectory={(path) => void openDirectory(selectedWorkspaceSlug, selectedAgentSlug, path)}
                    onDocument={(path) => void openDocument(path)}
                    onContent={setEditorContent}
                    onAddFileOpen={setAddFileOpen}
                    onNewFolder={setNewDocumentFolder}
                    onNewName={setNewDocumentName}
                    onBeginNew={beginNewDocument}
                    onCancelAddFile={() => {
                      setAddFileOpen(false);
                      setNewDocumentFolder("");
                      setNewDocumentName("");
                    }}
                    onEdit={() => setEditingDocument(true)}
                    onCancelEdit={cancelDocumentEdit}
                    onSave={() => void saveDocument()}
                    onDelete={() => void deleteDocument()}
                  />
                )}

                {tab === "access" && selectedAgent.can_manage && (
                  <AccessPanel
                    agent={selectedAgent}
                    members={agentAccess}
                    currentPrincipalId={me?.principal_id ?? ""}
                    currentWorkspaceRole={selectedWorkspace?.role ?? "member"}
                    allowAdminSelfGrant={me?.allow_admin_self_grant ?? false}
                    busy={busy}
                    onAccessPolicy={(policy) => void handleAgentAccessPolicy(policy)}
                    onRole={(member, role) => void applyContentRole(member, role)}
                    onManager={(member, next) => void handleManagerChange(member, next)}
                    onTransfer={(member) => void handleManagerTransfer(member)}
                  />
                )}

                {tab === "members" && workspaceAdmin && (
                  <MembersPanel
                    members={members}
                    invitations={invitations}
                    currentPrincipalId={me?.principal_id ?? ""}
                    owner={Boolean(workspaceOwner)}
                    inviteEmail={inviteEmail}
                    inviteRole={inviteRole}
                    busy={busy}
                    onInviteEmail={setInviteEmail}
                    onInviteRole={setInviteRole}
                    onInvite={() => void handleInvite()}
                    onRevoke={(invitation) => {
                      void runMutation(
                        `invite-revoke-${invitation.invitation_id}`,
                        async () => {
                          await revokeInvitation(getToken, selectedWorkspaceSlug, invitation.invitation_id);
                          await Promise.all([refreshMembers(selectedWorkspaceSlug), refreshAudit(selectedWorkspaceSlug)]);
                        },
                        `Invitation for ${invitation.email} revoked.`,
                      ).catch(() => undefined);
                    }}
                    onRole={(member, role) => {
                      void runMutation(
                        `member-role-${member.principal_id}`,
                        async () => {
                          await updateMemberRole(getToken, selectedWorkspaceSlug, member.principal_id, role);
                          await Promise.all([refreshMembers(selectedWorkspaceSlug), refreshWorkspaces(), refreshAudit(selectedWorkspaceSlug)]);
                        },
                        `${principalLabel(member)} is now a workspace ${role}.`,
                      ).catch(() => undefined);
                    }}
                    onTransfer={(member) => {
                      if (!window.confirm(`Transfer workspace ownership to ${principalLabel(member)}? You will become an administrator.`)) return;
                      void runMutation(
                        `member-transfer-${member.principal_id}`,
                        async () => {
                          await transferWorkspaceOwnership(getToken, selectedWorkspaceSlug, member.principal_id);
                          await Promise.all([refreshMembers(selectedWorkspaceSlug), refreshWorkspaces(), refreshAudit(selectedWorkspaceSlug)]);
                        },
                        `Workspace ownership transferred to ${principalLabel(member)}.`,
                      ).catch(() => undefined);
                    }}
                    onRemove={(member) => {
                      if (!window.confirm(`Remove ${principalLabel(member)} from this workspace? Their agent grants will also be removed.`)) return;
                      void runMutation(
                        `member-remove-${member.principal_id}`,
                        async () => {
                          await removeMember(getToken, selectedWorkspaceSlug, member.principal_id);
                          await Promise.all([refreshMembers(selectedWorkspaceSlug), refreshWorkspaces(), refreshAgentAccess(selectedWorkspaceSlug, selectedAgentSlug), refreshAudit(selectedWorkspaceSlug)]);
                        },
                        `${principalLabel(member)} removed from the workspace.`,
                      ).catch(() => undefined);
                    }}
                  />
                )}

                {tab === "audit" && workspaceAdmin && <AuditPanel events={audit} members={members} onRefresh={() => void refreshAudit(selectedWorkspaceSlug).catch(fail)} />}
              </>
            )}
          </section>
        </div>
      </main>

      {selfGrant && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4" role="dialog" aria-modal="true" aria-labelledby="self-grant-title">
          <div className="w-full max-w-md rounded-xl bg-white p-6 shadow-2xl dark:bg-gray-900">
            <div className="mb-4 flex items-start gap-3">
              <div className="rounded-full bg-amber-100 p-2 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300"><AlertTriangle className="h-6 w-6" /></div>
              <div>
                <h2 id="self-grant-title" className="text-lg font-bold">Grant yourself memory access?</h2>
                <p className="mt-2 text-sm text-gray-600 dark:text-gray-300">
                  This is an explicit permission escalation. It will be recorded in the workspace audit log. Once granted, the service can decrypt and return this agent&apos;s memory to your authenticated session.
                </p>
                <p className="mt-3 text-sm font-medium">Requested role: {contentRoleLabel(selfGrant.role)}</p>
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <button className={`${BUTTON} bg-gray-100 text-gray-800 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200`} onClick={() => setSelfGrant(null)}>Cancel</button>
              <button className={`${BUTTON} bg-amber-600 text-white hover:bg-amber-700`} onClick={() => void confirmSelfGrant()}><KeyRound className="h-4 w-4" /> Grant access</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

interface MemoryPanelProps {
  agent: AgentSummary;
  directory: string;
  entries: MemoryEntry[];
  document: MemoryDocument | null;
  editorContent: string;
  editorPath: string;
  creatingDocument: boolean;
  addFileOpen: boolean;
  newDocumentFolder: string;
  newDocumentName: string;
  editingDocument: boolean;
  editorDirty: boolean;
  busy: string;
  canWrite: boolean;
  canDelete: boolean;
  selfGrantAllowed: boolean;
  currentMember: MemberAccess | null;
  onSelfGrant: (member: MemberAccess) => void;
  onDirectory: (path: string) => void;
  onDocument: (path: string) => void;
  onContent: (content: string) => void;
  onAddFileOpen: (open: boolean) => void;
  onNewFolder: (folder: string) => void;
  onNewName: (name: string) => void;
  onBeginNew: () => void;
  onCancelAddFile: () => void;
  onEdit: () => void;
  onCancelEdit: () => void;
  onSave: () => void;
  onDelete: () => void;
}

function MemoryPanel(props: MemoryPanelProps) {
  if (!props.agent.content_role) {
    const currentMember = props.currentMember;
    return (
      <EmptyStateWithAction
        icon={<LockKeyhole className="h-7 w-7" />}
        title="Memory content is not available to you"
        body="Management access lets you administer this namespace without reading or changing its encrypted memory. Use the Access tab to assign content permission."
      >
        {props.selfGrantAllowed && currentMember ? (
          <button className={`${BUTTON} mt-4 bg-amber-600 text-white hover:bg-amber-700`} onClick={() => props.onSelfGrant(currentMember)}><KeyRound className="h-4 w-4" /> Grant myself reader access</button>
        ) : null}
      </EmptyStateWithAction>
    );
  }

  const segments = props.directory.split("/").filter(Boolean);
  const breadcrumbs = [{ label: "root", path: "/" }];
  let built = "";
  for (const segment of segments) {
    built += `/${segment}`;
    breadcrumbs.push({ label: segment, path: built });
  }

  return (
    <div className="grid min-h-[520px] lg:grid-cols-[300px_minmax(0,1fr)]">
      <div className="border-b border-gray-200 dark:border-gray-800 lg:border-b-0 lg:border-r">
        <div className="flex flex-wrap items-center gap-1 border-b border-gray-200 px-3 py-2 dark:border-gray-800">
          {breadcrumbs.map((crumb, index) => (
            <span key={crumb.path} className="flex items-center gap-1">
              {index > 0 && <ChevronRight className="h-3 w-3 text-gray-400" />}
              <button className="max-w-24 truncate text-xs text-blue-700 hover:underline dark:text-blue-300" onClick={() => props.onDirectory(crumb.path)}>{crumb.label}</button>
            </span>
          ))}
          <button aria-label="Refresh directory" className="ml-auto text-gray-400 hover:text-blue-600" onClick={() => props.onDirectory(props.directory)}><RefreshCw className={`h-4 w-4 ${props.busy === "memory-load" ? "animate-spin" : ""}`} /></button>
        </div>
        {props.canWrite && (
          <div className="border-b border-gray-200 p-2 dark:border-gray-800">
            {!props.addFileOpen ? (
              <button
                className={`${BUTTON} w-full border border-dashed border-blue-300 bg-blue-50 text-blue-700 hover:border-blue-400 hover:bg-blue-100 dark:border-blue-800 dark:bg-blue-950/30 dark:text-blue-300`}
                onClick={() => {
                  props.onNewFolder(
                    props.directory === "/"
                      ? ""
                      : props.directory.replace(/^\/+/, ""),
                  );
                  props.onAddFileOpen(true);
                }}
              >
                <Plus className="h-4 w-4" /> Add file
              </button>
            ) : (
              <form
                className="space-y-2 rounded-lg bg-gray-50 p-2 dark:bg-gray-950/50"
                onSubmit={(event) => {
                  event.preventDefault();
                  props.onBeginNew();
                }}
              >
                <div className="grid grid-cols-2 gap-2">
                  <input
                    aria-label="Folder for new file"
                    className={INPUT}
                    value={props.newDocumentFolder}
                    onChange={(event) => props.onNewFolder(event.target.value)}
                    placeholder="Folder (opt.)"
                  />
                  <input
                    aria-label="New file name"
                    autoFocus
                    className={INPUT}
                    value={props.newDocumentName}
                    onChange={(event) => props.onNewName(event.target.value)}
                    placeholder="filename.md"
                  />
                </div>
                <p className="px-1 text-[11px] text-gray-500 dark:text-gray-400">
                  Folder path is from root. Leave it blank to add the file at root.
                </p>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    className={`${BUTTON} bg-white text-gray-700 hover:bg-gray-100 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800`}
                    onClick={props.onCancelAddFile}
                  >
                    Cancel
                  </button>
                  <button
                    type="submit"
                    className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`}
                    disabled={!props.newDocumentName.trim()}
                  >
                    <FilePlus2 className="h-4 w-4" /> Continue
                  </button>
                </div>
              </form>
            )}
          </div>
        )}
        <div className="max-h-80 overflow-y-auto p-2 lg:max-h-[470px]">
          {props.entries.map((entry) => (
            <button key={`${entry.kind}:${entry.path}`} className="flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm hover:bg-gray-100 dark:hover:bg-gray-800" onClick={() => entry.kind === "directory" ? props.onDirectory(entry.path) : props.onDocument(entry.path)}>
              {entry.kind === "directory" ? <Folder className="h-4 w-4 shrink-0 text-amber-500" /> : <File className="h-4 w-4 shrink-0 text-blue-500" />}
              <span className="min-w-0 flex-1 truncate">{entry.name}</span>
              {entry.kind === "document" && <span className="text-xs text-gray-400">v{entry.version}</span>}
            </button>
          ))}
          {props.entries.length === 0 && <p className="p-4 text-center text-sm text-gray-500">This folder is empty.</p>}
        </div>
      </div>

      <div className="min-w-0">
        {!props.document && !props.creatingDocument ? (
          <EmptyState icon={<BookOpen className="h-7 w-7" />} title="Choose a memory document" body="Select a Markdown document from the file tree to read it." />
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-2 border-b border-gray-200 px-4 py-3 dark:border-gray-800">
              <span className="min-w-0 flex-1 truncate font-mono text-sm">{props.editorPath}</span>
              {props.document && <RoleBadge>v{props.document.version}</RoleBadge>}
              {props.document && props.canWrite && !props.editingDocument && (
                <button className={`${BUTTON} bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700`} onClick={props.onEdit}>
                  <Pencil className="h-4 w-4" /> Edit
                </button>
              )}
              {props.editingDocument && (
                <button className={`${BUTTON} bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700`} onClick={props.onCancelEdit}>
                  Cancel
                </button>
              )}
              {props.canWrite && props.editingDocument && (
                <button className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`} disabled={!props.editorDirty || props.busy === "document-save"} onClick={props.onSave}>{props.busy === "document-save" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} {props.creatingDocument ? "Create file" : "Save"}</button>
              )}
              {props.canDelete && props.document && !props.editingDocument && (
                <button aria-label="Delete document" className={`${BUTTON} bg-red-50 text-red-700 hover:bg-red-100 dark:bg-red-950/40 dark:text-red-300`} disabled={props.busy === "document-delete"} onClick={props.onDelete}>
                  {props.busy === "document-delete" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />} Delete file
                </button>
              )}
            </div>
            {props.editingDocument ? (
              <textarea
                aria-label={`Edit ${props.editorPath}`}
                autoFocus={props.creatingDocument}
                className="min-h-[450px] w-full resize-y bg-transparent p-5 font-mono text-sm leading-6 outline-none"
                value={props.editorContent}
                onChange={(event) => props.onContent(event.target.value)}
                spellCheck={false}
              />
            ) : (
              <div className="markdown-preview min-h-[450px] max-w-none overflow-auto p-5">
                {props.editorContent ? (
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{props.editorContent}</ReactMarkdown>
                ) : (
                  <p className="text-sm italic text-gray-500 dark:text-gray-400">This file is empty.</p>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyStateWithAction({ icon, title, body, children }: { icon: ReactNode; title: string; body: string; children?: ReactNode }) {
  return (
    <div className="flex min-h-96 flex-col items-center justify-center px-6 py-10 text-center">
      <div className="mb-3 rounded-full bg-gray-100 p-3 text-gray-500 dark:bg-gray-800 dark:text-gray-400">{icon}</div>
      <h3 className="font-semibold text-gray-900 dark:text-white">{title}</h3>
      <p className="mt-1 max-w-md text-sm text-gray-500 dark:text-gray-400">{body}</p>
      {children}
    </div>
  );
}

function AccessPanel({ agent, members, currentPrincipalId, currentWorkspaceRole, allowAdminSelfGrant, busy, onAccessPolicy, onRole, onManager, onTransfer }: {
  agent: AgentSummary;
  members: MemberAccess[];
  currentPrincipalId: string;
  currentWorkspaceRole: WorkspaceRole;
  allowAdminSelfGrant: boolean;
  busy: string;
  onAccessPolicy: (policy: AgentAccessPolicy) => void;
  onRole: (member: MemberAccess, role: ContentRole | null) => void;
  onManager: (member: MemberAccess, next: boolean) => void;
  onTransfer: (member: MemberAccess) => void;
}) {
  const workspaceAdmin = currentWorkspaceRole === "owner" || currentWorkspaceRole === "admin";
  const workspaceRead = agent.access_policy === "workspace_read";
  const canChangePolicy = workspaceRead || Boolean(agent.content_role) || (workspaceAdmin && allowAdminSelfGrant);
  return (
    <div className="p-5">
      <div className="mb-5 flex flex-col gap-4 rounded-lg border border-gray-200 bg-gray-50 p-4 dark:border-gray-800 dark:bg-gray-950/40 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex gap-3">
          <div className={`mt-0.5 rounded-full p-2 ${workspaceRead ? "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300" : "bg-gray-200 text-gray-600 dark:bg-gray-800 dark:text-gray-300"}`}>
            {workspaceRead ? <Users className="h-5 w-5" /> : <LockKeyhole className="h-5 w-5" />}
          </div>
          <div>
            <p className="font-semibold">Workspace read-only access</p>
            <p className="mt-1 max-w-2xl text-sm text-gray-600 dark:text-gray-300">
              {workspaceRead
                ? "Every current and future workspace member can read this store. Editing and deletion still require an explicit individual role."
                : "Only people with an explicit individual content role can read this store."}
            </p>
            {!canChangePolicy && (
              <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">You need content access to share this store. Administrator self-grants are disabled.</p>
            )}
          </div>
        </div>
        <button
          aria-label="Share this memory store read-only with the workspace"
          aria-pressed={workspaceRead}
          className={`${BUTTON} min-w-36 shrink-0 ${workspaceRead ? "bg-blue-600 text-white hover:bg-blue-700" : "border border-gray-300 bg-white text-gray-700 hover:bg-gray-100 dark:border-gray-700 dark:bg-gray-900 dark:text-gray-200 dark:hover:bg-gray-800"}`}
          disabled={!canChangePolicy || busy === "agent-access-policy"}
          onClick={() => onAccessPolicy(workspaceRead ? "private" : "workspace_read")}
        >
          {busy === "agent-access-policy" ? <Loader2 className="h-4 w-4 animate-spin" /> : workspaceRead ? <Users className="h-4 w-4" /> : <LockKeyhole className="h-4 w-4" />}
          {workspaceRead ? "Shared read-only" : "Private"}
        </button>
      </div>
      <div className="mb-5 rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-900 dark:border-blue-900 dark:bg-blue-950/40 dark:text-blue-200">
        <div className="flex gap-3"><Shield className="h-5 w-5 shrink-0" /><div><p className="font-semibold">Management and memory are independent</p><p className="mt-1">A manager does not receive content access automatically. Individual roles are explicit; workspace-wide access, when enabled above, is read-only.</p></div></div>
      </div>
      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800">
        <table className="w-full min-w-[780px] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500 dark:bg-gray-950/50"><tr><th className="px-4 py-3">Member</th><th className="px-4 py-3">Workspace</th><th className="px-4 py-3">Manage agent</th><th className="px-4 py-3">Memory content</th><th className="px-4 py-3 text-right">Transfer</th></tr></thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
            {members.map((member) => {
              const self = member.principal_id === currentPrincipalId;
              const inherentManager = member.workspace_role === "owner" || member.workspace_role === "admin";
              const canChooseSelfRole = !self || (workspaceAdmin && allowAdminSelfGrant);
              return (
                <tr key={member.principal_id}>
                  <td className="px-4 py-3"><p className="font-medium">{principalLabel(member)} {self && <span className="text-xs text-gray-400">(you)</span>}</p><p className="text-xs text-gray-500">{member.email || member.principal_id}</p></td>
                  <td className="px-4 py-3"><RoleBadge>{workspaceRoleLabel(member.workspace_role)}</RoleBadge></td>
                  <td className="px-4 py-3">
                    {inherentManager ? <span className="text-xs text-gray-500">Included in workspace role</span> : self ? (
                      <span className="text-xs text-gray-500">{member.explicit_manager ? "Explicit manager" : "Not manager"}</span>
                    ) : (
                      <label className="inline-flex items-center gap-2"><input type="checkbox" checked={member.explicit_manager} disabled={busy === `manager-${member.principal_id}`} onChange={(event) => onManager(member, event.target.checked)} /><span>{member.explicit_manager ? "Manager" : "Not manager"}</span></label>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <select className={INPUT} value={member.content_role ?? ""} disabled={busy === `content-${member.principal_id}`} onChange={(event) => onRole(member, (event.target.value || null) as ContentRole | null)}>
                      <option value="">{workspaceRead ? "Workspace reader (inherited)" : "No content access"}</option>
                      <option value="reader" disabled={self && !canChooseSelfRole}>Reader</option>
                      <option value="editor" disabled={self && !canChooseSelfRole}>Editor</option>
                      <option value="full_access" disabled={self && !canChooseSelfRole}>Full access</option>
                    </select>
                    {!member.content_role && member.effective_content_role === "reader" && <p className="mt-1 text-xs text-blue-700 dark:text-blue-300">Inherited from workspace access</p>}
                    {self && !canChooseSelfRole && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">Self-grant is disabled. You may still revoke existing access.</p>}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {!self && (
                      <button className={`${BUTTON} bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-200`} disabled={busy === `manager-transfer-${member.principal_id}`} onClick={() => onTransfer(member)} title={workspaceAdmin ? "Assign explicit management; your workspace role remains unchanged" : "Transfer your explicit management authority"}>
                        <UserCog className="h-4 w-4" /> Transfer
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MembersPanel({ members, invitations, currentPrincipalId, owner, inviteEmail, inviteRole, busy, onInviteEmail, onInviteRole, onInvite, onRevoke, onRole, onTransfer, onRemove }: {
  members: MemberAccess[];
  invitations: InvitationSummary[];
  currentPrincipalId: string;
  owner: boolean;
  inviteEmail: string;
  inviteRole: "admin" | "member";
  busy: string;
  onInviteEmail: (value: string) => void;
  onInviteRole: (value: "admin" | "member") => void;
  onInvite: () => void;
  onRevoke: (invitation: InvitationSummary) => void;
  onRole: (member: MemberAccess, role: "admin" | "member") => void;
  onTransfer: (member: MemberAccess) => void;
  onRemove: (member: MemberAccess) => void;
}) {
  return (
    <div className="space-y-6 p-5">
      <div>
        <h3 className="font-semibold">Add a workspace member</h3>
        <p className="mt-1 text-sm text-gray-500">Membership does not grant private memory access. Members can read only stores that are explicitly shared workspace-wide or granted to them individually.</p>
        <div className="mt-3 grid gap-2 sm:grid-cols-[minmax(0,1fr)_140px_auto]">
          <input className={INPUT} type="email" value={inviteEmail} onChange={(event) => onInviteEmail(event.target.value)} placeholder="person@example.com" />
          <select className={INPUT} value={inviteRole} onChange={(event) => onInviteRole(event.target.value as "admin" | "member")}><option value="member">Member</option>{owner && <option value="admin">Administrator</option>}</select>
          <button className={`${BUTTON} bg-blue-600 text-white hover:bg-blue-700`} disabled={!inviteEmail.trim() || busy === "member-invite"} onClick={onInvite}><UserPlus className="h-4 w-4" /> Add</button>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-800">
        <table className="w-full min-w-[680px] text-left text-sm">
          <thead className="bg-gray-50 text-xs uppercase text-gray-500 dark:bg-gray-950/50"><tr><th className="px-4 py-3">Member</th><th className="px-4 py-3">Workspace role</th><th className="px-4 py-3 text-right">Actions</th></tr></thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
            {members.map((member) => {
              const self = member.principal_id === currentPrincipalId;
              const canRemove = !self && member.workspace_role !== "owner" && (owner || member.workspace_role === "member");
              return (
                <tr key={member.principal_id}>
                  <td className="px-4 py-3"><p className="font-medium">{principalLabel(member)} {self && <span className="text-xs text-gray-400">(you)</span>}</p><p className="text-xs text-gray-500">{member.email || member.principal_id}</p></td>
                  <td className="px-4 py-3">
                    {owner && member.workspace_role !== "owner" ? (
                      <select className={`${INPUT} max-w-44`} value={member.workspace_role} disabled={busy === `member-role-${member.principal_id}`} onChange={(event) => onRole(member, event.target.value as "admin" | "member")}><option value="member">Member</option><option value="admin">Administrator</option></select>
                    ) : <RoleBadge>{workspaceRoleLabel(member.workspace_role)}</RoleBadge>}
                  </td>
                  <td className="px-4 py-3"><div className="flex justify-end gap-2">{owner && !self && <button className={`${BUTTON} bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200`} onClick={() => onTransfer(member)}><UserCog className="h-4 w-4" /> Transfer ownership</button>}{canRemove && <button aria-label={`Remove ${principalLabel(member)}`} className={`${BUTTON} bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300`} onClick={() => onRemove(member)}><Trash2 className="h-4 w-4" /></button>}</div></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {invitations.length > 0 && (
        <div>
          <h3 className="mb-2 font-semibold">Pending invitations</h3>
          <div className="divide-y divide-gray-200 rounded-lg border border-gray-200 dark:divide-gray-800 dark:border-gray-800">
            {invitations.map((invitation) => (
              <div key={invitation.invitation_id} className="flex items-center gap-3 px-4 py-3 text-sm"><div className="min-w-0 flex-1"><p className="truncate font-medium">{invitation.email}</p><p className="text-xs text-gray-500">Invited as {invitation.role}</p></div><button className={`${BUTTON} bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200`} disabled={busy === `invite-revoke-${invitation.invitation_id}`} onClick={() => onRevoke(invitation)}>Revoke</button></div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AuditPanel({ events, members, onRefresh }: { events: ManagementEvent[]; members: MemberAccess[]; onRefresh: () => void }) {
  const labels = new Map(members.map((member) => [member.principal_id, principalLabel(member)]));
  return (
    <div className="p-5">
      <div className="mb-4 flex items-center justify-between"><div><h3 className="font-semibold">Management audit</h3><p className="text-sm text-gray-500">Content-free records of workspace and access changes.</p></div><button className={`${BUTTON} bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200`} onClick={onRefresh}><RefreshCw className="h-4 w-4" /> Refresh</button></div>
      <div className="space-y-2">
        {events.map((event) => (
          <div key={event.event_id} className="rounded-lg border border-gray-200 p-3 text-sm dark:border-gray-800"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-mono text-xs font-semibold text-blue-700 dark:text-blue-300">{event.action}</span><span className="text-xs text-gray-500">{displayDate(event.occurred_at)}</span></div><p className="mt-2 text-gray-600 dark:text-gray-300"><span className="font-medium">{labels.get(event.actor_principal_id) || event.actor_principal_id}</span> changed {event.target_kind} <span className="font-mono text-xs">{labels.get(event.target_id) || event.target_id}</span>.</p></div>
        ))}
        {events.length === 0 && <EmptyState icon={<History className="h-7 w-7" />} title="No management events" body="Workspace and access changes will appear here without memory content." />}
      </div>
    </div>
  );
}
