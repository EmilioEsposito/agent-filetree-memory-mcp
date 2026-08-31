# Bundled management UI

The `web` extra ships a prebuilt React interface in the Python wheel. End users
do not need Node or Vite. The same package version supplies the management API,
static assets, and route composition helpers, which prevents frontend/backend
version drift.

```shell
pip install 'agent-filetree-memory-mcp[web,mcp]'
```

An embedding service constructs `NamespaceStore`, `ManagementStore`, and
`MemoryService` from the same injected async SQLAlchemy session factory. It
then calls `create_management_api(...)` with a FastAPI dependency that returns
a verified `ManagementPrincipal`, and mounts the result with
`create_web_application(...)` or `create_management_frontend(...)`.

The recommended one-process layout is:

```text
/api  authenticated management API
/mcp  host-authenticated MCP transport
/ui   bundled management frontend
```

All paths are configurable, and the combined app may itself be mounted beneath
a service prefix. The host owns schema creation, Alembic execution, database
credentials, identity verification, key resolution, and TLS.

For the selected workspace and agent, the UI derives the durable MCP connection
URL from `ManagementFrontendConfig.mcp_base_url` and provides a copy button.
The default `../mcp` value follows the recommended one-process layout while
remaining relative to any host-selected service prefix.

## Browser authentication modes

`FrontendAuthConfig` supports three deployment-neutral modes:

- `oidc`: use Authorization Code with PKCE through a configured public client.
  The issuer, client ID, scopes, and token field are public browser metadata;
  the management API must still verify every bearer token independently.
- `session`: rely on an authenticated same-origin cookie established by the
  embedding service or reverse proxy.
- `none`: send no browser credential. This is suitable only when the API uses a
  fixed local principal or another enclosing trusted boundary.

The UI never receives a client secret. Its runtime `config.json` is non-secret,
not cached, and restricted to same-origin API URLs. OIDC redirect URIs must be
registered for the exact externally visible `/ui/` URL.

## Authorization and decryption

Workspace owners and administrators can see agent existence and slugs. Agent
management permission is independent from content permission, so they may
rename agents, transfer ownership, and manage grants without reading memory.
Creating an agent is the intentional exception: the creator receives an
explicit full-content grant in the same transaction as the new namespace.
That grant can later be changed or revoked like any other content grant.
The deployment decides whether an administrator may explicitly grant their own
account reader/editor/full access. When enabled, the UI shows a warning before
the API performs and audits that self-grant.

The UI has no decryption shortcut. It invokes the same authorization-first
`MemoryService` used by MCP, and encrypted content is decrypted only after the
current principal has an applicable content grant.
