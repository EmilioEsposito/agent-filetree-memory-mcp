# Standalone stdio server

The `agent-filetree-memory-mcp` console script runs the same package as a
headless stdio MCP server. It owns an async SQLAlchemy engine created from an
explicit PostgreSQL URL. It does not fetch credentials, create a schema, or run
DDL. Platforms with rotating credentials should embed the package through
`PostgresRuntime.from_session_factory(...)` instead.

Install the complete package extra:

```shell
pip install 'agent-filetree-memory-mcp[all]'
```

Nothing has been published yet. A locally built wheel behaves the same way:

```shell
uv build
python -m venv /tmp/agent-filetree-memory-wheel
/tmp/agent-filetree-memory-wheel/bin/pip install 'dist/agent_filetree_memory_mcp-0.1.0-py3-none-any.whl[all]'
```

## Database prerequisite

Before starting the server, create the selected PostgreSQL schema through your
normal database-administration process and apply the package's Alembic branch.
The host Alembic environment must call
`configure_host_alembic(config, schema="agent_filetree_memory")` and upgrade to
`agent_filetree_memory@head`. See the packaged
`agent_filetree_memory.postgres.migrations` module and its README for the exact
host integration. Server startup intentionally performs no migration or schema
creation.

## Required settings

All secrets must come from the launcher environment or its secret manager. The
server never supplies development defaults for them.

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | PostgreSQL URL. Plain `postgresql://` is normalized to the asyncpg driver. |
| `AGENT_FILETREE_MEMORY_KEYRING_JSON` | JSON object mapping opaque key IDs to standard-base64 32-byte AES keys. Keep retired keys for reads. |
| `AGENT_FILETREE_MEMORY_ACTIVE_KEY_ID` | Keyring entry used to wrap new per-version data keys. |
| `AGENT_FILETREE_MEMORY_IDEMPOTENCY_INDEX_KEY` | Standard-base64 encoding of a separate 32-byte HMAC key, stable across restarts. |
| `AGENT_FILETREE_MEMORY_CAPABILITY_TOKEN` | Pre-issued short-lived capability for the one verified agent exposed by this process. |
| `AGENT_FILETREE_MEMORY_CAPABILITY_PUBLIC_KEY_FILE` | Local PEM file containing the trusted Ed25519 public key. |
| `AGENT_FILETREE_MEMORY_CAPABILITY_KEY_ID` | Expected capability signing key ID. |
| `AGENT_FILETREE_MEMORY_CAPABILITY_ISSUER` | Exact trusted issuer. |
| `AGENT_FILETREE_MEMORY_CAPABILITY_AUDIENCE` | Exact audience for this server. |
| `AGENT_FILETREE_MEMORY_PRINCIPAL_ID` | Authenticated principal that must match the capability. |

Optional settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_FILETREE_MEMORY_DATABASE_SCHEMA` | `agent_filetree_memory` | Existing migrated PostgreSQL schema. |
| `AGENT_FILETREE_MEMORY_SERVICE_NAMESPACE` | `agent-filetree-memory` | Namespace used for persistent operation indexes. |
| `AGENT_FILETREE_MEMORY_ENABLE_APP` | `false` | Include the bundled MCP App browser/editor. Accepts `true` or `false`. |

The static capability is still short-lived and is reverified on every tool
call. This launcher is suitable for a locally supervised or per-session
process. Long-running hosted services should inject a request-aware invocation
resolver instead of relying on one environment-provided token.

Start the stdio process only after the migration and configuration are in place:

```shell
agent-filetree-memory-mcp
```

## Host-operated retention janitor

The MCP process deliberately does not run maintenance in its request-serving
runtime. Schedule the separate one-shot command with cron, a Kubernetes
CronJob, or the host platform's equivalent:

```shell
agent-filetree-memory-janitor
```

The janitor uses only `DATABASE_URL` and
`AGENT_FILETREE_MEMORY_DATABASE_SCHEMA`; it does not accept encryption,
idempotency-index, capability-signing, or capability-token credentials. Each
run emits a content-free JSON count and processes at most 100 selected rows
from each lifecycle table by default. A backlog larger than one batch requires
repeated runs.

Optional maintenance settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_FILETREE_MEMORY_JANITOR_BATCH_LIMIT` | `100` | Maximum rows selected directly from each lifecycle table in one transaction. |
| `AGENT_FILETREE_MEMORY_JANITOR_AUDIT_RETENTION_DAYS` | `90` | Retention assigned to content-free hard-purge audit events. |

`purge_after` and `expires_at` only mark rows as eligible. They do not delete
anything until this command (or `PostgresJanitor` in an embedding host) runs.
The store also keeps at most 32 ciphertext versions per object by default;
hosts can set `PostgresStoreConfig.max_versions_per_object` explicitly. That
safety ceiling can prune the oldest superseded version before its ordinary
retention deadline. Database backups follow the host's separate expiry policy.
