# Authentication and durable agent identity

Agent Filetree Memory separates two security questions that MCP deployments
often combine accidentally:

1. **Transport authentication:** which human, service, or MCP client may connect?
2. **Memory capability:** which durable agent profile may this invocation access,
   and which operations may it perform?

MCP authorization solves the first question through the host's OAuth or bearer
token implementation. MCP does not currently define a portable, authenticated,
durable agent-profile identity. `clientInfo`, initialization metadata, tool
arguments, display names, and `MCP-Session-Id` are not substitutes: they are
self-asserted, model-controlled, mutable, or scoped only to one transport
session.

## Bring your own transport authentication

`create_mcp_server(..., auth=...)` accepts the hosting FastMCP authentication
provider. The package has no dependency on a particular identity provider. A
host can use Microsoft Entra ID, another OAuth 2.1 provider, a service-to-service
verifier, or a local test provider without changing the memory application or
PostgreSQL adapter.

The host must reduce the validated identity to an opaque, stable
`principal_id`. Do not use an unverified email address, display name, URL
parameter, or model argument for this value.

## Signed invocation capabilities

After transport authentication, a trusted agent host issues a short-lived
Ed25519-signed capability. The capability binds all of the following:

- the outer authenticated `principal_id`;
- an opaque `workspace_id`;
- an immutable, durable `agent_profile_id`;
- invocation, capability, issuer, and audience identifiers;
- allowed memory actions, expiry, and delegation depth.

The memory server verifies the signature and requires the capability's
`principal_id` to match the current transport-authenticated principal before it
constructs `VerifiedInvocation`. Scope identifiers never appear in model or MCP
App tool schemas.

The durable scope is exactly `(workspace_id, agent_profile_id)`. The
same verified agent profile therefore sees the same memory across reconnects and
conversations. A host can issue a fresh invocation capability for every run
without changing that memory scope.

`principal_id` is deliberately not part of that durable scope. It identifies
the authenticated actor for the current capability. The package's optional
control plane stores workspace membership, profile visibility, invitations,
management grants, content grants, and content-free audit events. The host
still owns identity verification and supplies the stable principal; it may use
the package control plane or enforce an equivalent external policy.

When a write or append creates an immutable document version, that verified
`principal_id` is stored with the encrypted version and returned as
`committed_by` with `verification: "authenticated"`. This is intentionally a
narrow statement: the principal's authenticated authorization committed the
version. It is not proof that a particular human drafted the text, that an
agent acted autonomously, or that the principal approved every line.

A caller may additionally declare opaque `co_authored_by` identifiers, similar
in spirit to commit co-author trailers. Those identifiers are not resolved
through transport authentication and are always returned with
`verification: "self_asserted"`. The optional `change_comment` is likewise
caller-supplied descriptive text rather than verified authorship. Older
versions that predate the metadata framing return no committer or co-author
claim rather than guessing from later audit records.

## Provisioning a connection for a generic MCP client

A generic MCP client may be unable to mint signed per-run agent context. The
recommended v1 host flow uses human-entered aliases directly in the configured
MCP endpoint, with no required setup page:

`/mcp/workspaces/{workspace_slug}/agents/{agent_slug}`

The workspace slug is unique within one deployment or control plane. The agent
slug is unique within that workspace. On every connection, the host:

1. authenticates the principal through its configured identity provider;
2. resolves the workspace alias to an immutable `workspace_id` and verifies
   workspace access;
3. atomically resolves the agent alias to an immutable `agent_profile_id`;
4. if the agent alias is absent, creates the profile only when that principal
   has workspace permission to create agent profiles; and
5. issues a short-lived capability for the resolved immutable scope and actor.

An existing alias always reconnects to the same durable profile, including
across MCP sessions and conversations. Renaming an alias never changes the
immutable IDs or moves memory. A missing or unauthorized workspace/agent route
should return the same generic not-found response so route probing cannot reveal
private profiles.

Slugs are non-secret selectors, not bearer credentials or authorization claims.
Knowing or changing a URL grants nothing without successful outer
authentication and server-side authorization. Choose slugs that are safe to
appear in configuration, browser history, proxy logs, and support diagnostics;
never place user IDs, immutable database IDs, secrets, or sensitive memory in
the URL.

`NamespaceStore.resolve_or_create(...)` implements the atomic authorization and
create-on-connect decision. The embedding host remains responsible for mapping
its authenticated request and route into that call; URL metadata by itself is
never trusted.

## Collaboration control plane

The optional control plane supports workspace owners, administrators, and
members; per-agent content roles; independent per-agent managers; invitations;
ownership transfer; and audit history. Management is deliberately not a
superset of read or write. A workspace administrator can enumerate agent slugs
and manage metadata without decrypting content. If deployment policy enables
administrator self-grants, the management API requires an explicit confirmation
and records the grant as a content-free audit event.

Platform administration is a host-verified capability rather than a stored
content role. Platform administrators alone may create workspaces and may list
workspace metadata across the deployment. They cannot enumerate a workspace's
agent slugs until they explicitly become a member or assign themselves its
administrator role, and that role still confers no content permission.

Workspace admission is independently configured as invite-only, open to every
authenticated principal, or delegated to an injected external-entitlement
resolver. Agent creation is independently limited to workspace administrators
or opened to all members. When an authorized principal creates a missing agent
through the API or its MCP URL, the profile, explicit manager row, full-content
grant, and content-free audit events commit atomically. Existing profiles are
never silently backfilled on reconnect.

There is intentionally no universal `agent_name` or `agent_profile_id` memory
tool argument. Names are mutable and model-controlled arguments are not an
authorization boundary. The URL aliases are supplied by the human while
configuring the connection, never accepted as memory-tool arguments. Switching
profiles requires connecting through another configured endpoint or receiving a
capability for another server-resolved profile from the trusted host.

## MCP App continuity

The optional App can browse and edit only the profile resolved for the current
authenticated request. It has no profile picker and its app-only helper
visibility is user-interface metadata, not an authorization boundary.

Every helper call reauthenticates and validates a signed app-instance binding to
the issuer, audience, principal, workspace, and agent profile. The binding
deliberately excludes conversation, invocation, and capability identifiers, so
the same agent's open UI remains valid after a short-lived capability refresh.
A different principal or agent profile must open its own App instance.

The bundled administrative web UI is a separate surface. It authenticates a
human through a host-supplied FastAPI dependency, then exposes only operations
authorized by the control plane. Browser OIDC configuration is public client
metadata and never replaces API-side token verification.
