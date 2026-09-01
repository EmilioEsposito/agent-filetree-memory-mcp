# Architecture

## Goals

The library provides durable, versioned Markdown-like memory without making a filesystem, Git repository, model, or MCP client the authorization boundary. It is designed to be embedded by a trusted agent host or run as a standalone MCP server.

The first release optimizes for five properties:

1. One verified invocation can access exactly one workspace and durable agent profile.
2. Human-readable paths and content do not appear in plaintext database rows or routine logs.
3. Writes are concurrency-safe and safely retryable.
4. Database credentials and key-management implementations remain deployer choices.
5. Headless MCP tools remain complete when a client does not support MCP Apps.

## Components

```text
trusted host
  -> capability verifier
  -> application service
       -> PostgreSQL memory store
            -> encrypted directory manifests
            -> encrypted immutable document versions
            -> content-free lifecycle and audit rows
       -> envelope cipher
            -> injected DEK provider
  -> MCP tools
       -> optional current-capability MCP App
  -> authenticated management API
       -> optional workspace and agent control plane
       -> same application service for memory reads and writes
  -> bundled management UI
```

The application layer receives an immutable `VerifiedInvocation`. It never accepts workspace or agent-profile identifiers alongside an operation. Transport adapters resolve the invocation from trusted request context before calling the service.

The durable namespace is `(workspace_id, agent_profile_id)`. `principal_id` is a
separately authenticated actor recorded in the signed invocation. The optional
control plane can authorize multiple workspace members to the same profile
without moving or duplicating its memory. Management permission is independent
from reader/editor/full content access, and workspace administrators may see
agent slugs without implicitly gaining decryption access. A deployer may allow
explicit, audited administrator self-grants or disable them as policy.
Agent creation atomically provisions an explicit manager row and full-content
grant for the creator; it does not grant content access to other workspace
administrators. Platform administration is injected by the host and reveals
only global workspace metadata until that principal explicitly takes a
workspace role. Workspace admission and agent-creation policies are signed
control-plane rows, with an injected provider-neutral resolver for external
entitlements.

## Database injection

The PostgreSQL adapter supports two construction paths:

- `PostgresRuntime.from_url(...)` creates and owns an async engine for a conventional static database URL.
- `PostgresRuntime.from_session_factory(...)` receives a ready-to-use async session factory and never creates, refreshes, or disposes host infrastructure.

This is intentionally a session-factory seam rather than a credential-fetcher seam. A hosting platform can rotate credentials, use workload identity, or retrieve secrets inside its own engine without coupling those mechanisms to this package.

## Encrypted virtual tree

Each scope has an opaque root object. Directories are objects whose version content is an encrypted manifest of child display names, opaque object identifiers, and kinds. Documents are objects whose immutable versions contain encrypted UTF-8 Markdown plus compact version metadata: the authenticated committing principal, caller-declared co-author identifiers, and an optional change comment. The metadata is inside the authenticated encrypted payload, so no new plaintext content or attribution index is introduced. Pre-metadata document ciphertext remains readable as legacy Markdown with unavailable attribution.

Resolving `/projects/example.md` therefore requires:

1. Selecting the opaque root for the already-authorized scope.
2. Decrypting its manifest and selecting the opaque `projects` object.
3. Decrypting that directory manifest and selecting the opaque document object.
4. Loading and decrypting the document's current immutable version.

The plaintext store can route by opaque scope and object identifiers, but it cannot enumerate human-readable paths or titles.

## Encryption

Every object version receives a fresh random 256-bit data-encryption key. AES-256-GCM encrypts the content with canonical associated data containing only format version and opaque scope, object, kind, and version identifiers. A `DekProvider` wraps the data key with the same context.

Rows retain the provider and key identifiers required for rotation. The bundled local keyring provider accepts explicit 32-byte keys and supports reading older keys while writing with one active key. Hosted key-management providers can implement the same small interface.

Moving ciphertext, a wrapped key, or a version row to another object or scope changes the authenticated context and must fail closed.

## History and provenance

`version_created_at` is the canonical immutable-version timestamp. Current
list/read payloads also expose the former `updated_at` name as a compatibility
alias, derived from the same version row rather than a separately sampled
object timestamp.

History access is split at the capability boundary:

- `memory:history:list` permits bounded, newest-first retained-version metadata;
- `memory:history:read` permits historical Markdown and an optional unified
  line diff from `compare_to_version` into the selected version.

Neither action is implied by `memory:read` at the capability layer. A host may
grant them together through a reader role, as the optional control plane does,
or issue narrower capabilities. Expired and pruned versions use the same
unavailable result shape rather than revealing whether a version once existed.
Because list metadata includes the free-text change comment requested for each
version, `memory:history:list` is content-adjacent rather than guaranteed
content-free and should be granted accordingly.

`committed_by` records the `principal_id` from the verified invocation that
created a version. It proves which authenticated authorization was exercised,
not which human or agent composed the content. `co_authored_by` is analogous to
a commit co-author declaration but is deliberately labeled `self_asserted`.
`change_comment` is optional caller-supplied description, not an identity claim.

## Concurrency and retries

Object heads are compare-and-swap values. `write`, `append`, and `delete` take an expected document version. One of two writes against the same version may succeed; the other receives a stable version-conflict error.

Mutations also require an idempotency key. The corresponding request fingerprint and result are encrypted. Co-author declarations and the change comment are part of that fingerprint. A retry of the same request returns the original result. Reusing the key for a different request fails without applying either request again.

## Deletion and retention

Delete removes the entry from its encrypted parent manifest and marks the object inaccessible in the same transaction. `purge_after` is an eligibility time, not proof that physical deletion has already run. The package never starts a background scheduler or service lifespan task. A deployer must invoke `agent-filetree-memory-janitor` from cron, a Kubernetes CronJob, or an equivalent host scheduler.

Each janitor invocation selects and locks at most its configured batch limit independently from deleted objects, superseded versions, expired idempotency records, expired rate buckets, and expired audit rows. Repeated invocations drain a larger backlog. Deleting a due object also cascades its versions; `max_versions_per_object` provides a deterministic ceiling on that cascade and on retained ciphertext history. The oldest version is pruned as a new version crosses the ceiling, even when its normal retention time has not arrived.

The janitor requires database access and schema selection, but not capability, idempotency-index, or encryption keys. Backup and replica expiry remain deployer responsibilities and may extend physical recoverability beyond application-table deletion.

## MCP App

The app is a convenience view over the same service layer. Its backend helpers receive paths, content, expected versions, and idempotency keys, but never scope identifiers or a capability token. Every app interaction resolves and reauthorizes the current invocation before lookup or decryption.

Plaintext drafts remain in ephemeral component memory. They are not placed in URLs, browser storage, model-visible context, or the initial widget bootstrap result.

## Management UI

The wheel contains a prebuilt React application plus a provider-neutral
FastAPI management API. The host injects the authenticated principal, database
session factory, encryption keys, MCP transport authentication, and policy.
The UI calls the same application service as MCP; it has no direct database or
privileged decryption path. FastAPI serves `/api`, `/mcp`, and `/ui` from one
process, so the frontend and API remain version-matched without requiring Node
at installation time.

The policy migration does not infer or backfill existing agent managers or
content grants. Existing agents retain their exact authorization rows; missing
workspace policy rows default to invite-only admission and administrator-only
agent creation.

## Deliberate non-goals

- Conversation transcript persistence
- Automatic memory extraction from prompts or messages
- Semantic or vector search
- Automated declassification or content-approval workflows
- Filesystem mounting or online Git synchronization
- Binary attachments or collaborative editing
- A deployer-independent user directory or identity provider
