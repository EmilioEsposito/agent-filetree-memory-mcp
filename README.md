# Agent Filetree Memory MCP

Agent Filetree Memory MCP is a framework-neutral memory service for agents. It stores a simple virtual Markdown file tree in PostgreSQL while keeping paths and document content encrypted at rest.

The project is designed around four boundaries:

- The host verifies a short-lived capability and selects the workspace and durable agent profile. Models and UI components cannot choose or widen those identifiers.
- Every document and directory version is immutable and encrypted with a fresh AES-256-GCM data key.
- PostgreSQL is injected through an async SQLAlchemy session factory. Standalone users may construct one from a static database URL; hosting platforms may supply their own credential-aware factory.
- MCP is an adapter. The application service can also be embedded directly in another Python service.

The initial tool surface is intentionally small: list, read, write, append, and delete. Writes use compare-and-swap versions and idempotency keys. Delete denies access immediately and makes encrypted data eligible for hard deletion after its configured retention window. The host must run the packaged janitor; the request-serving process does not schedule cleanup itself.

## Status

This repository is pre-release and local-only. No package or hosted service has been published.

## Intended package layers

- `agent_filetree_memory.domain`: dependency-light identifiers, paths, results, and errors.
- `agent_filetree_memory.application`: authorization-first memory operations.
- `agent_filetree_memory.crypto`: envelope encryption and pluggable data-key providers.
- `agent_filetree_memory.postgres`: PostgreSQL persistence and packaged Alembic migrations.
- `agent_filetree_memory.control_plane`: optional workspaces, durable agent profiles, membership, independent management/content grants, and audit.
- `agent_filetree_memory.mcp`: headless MCP tools.
- `agent_filetree_memory.mcp_app`: an optional current-capability browser and editor.
- `agent_filetree_memory.web`: the version-matched management API composition and bundled React UI.

## Security properties

The service authorizes before storage lookup or decryption. Missing and unauthorized documents share the same public failure shape. Database rows contain opaque routing, authorization, version, and lifecycle fields; human-readable paths, titles, tags, snippets, Markdown, and directory manifests are encrypted.

See [SECURITY.md](SECURITY.md) for the threat model and disclosure guidance.
See [docs/authentication.md](docs/authentication.md) for the two-layer transport
and durable agent-identity design.
See [docs/standalone.md](docs/standalone.md) for running the packaged stdio
server and retention janitor from a static PostgreSQL URL.
See [docs/management-ui.md](docs/management-ui.md) for mounting the bundled
administrative UI with a host-supplied identity dependency.

## License

Apache License 2.0. See [LICENSE](LICENSE).
