# Verification strategy

The security contract requires more than a happy-path unit test.

## Local commands

`uv sync --locked` installs development dependencies. `uv run --locked pytest`
runs tests that need no external service. With Docker running,
`uv run python -m devtools.postgres` provisions a disposable PostgreSQL instance
and runs the complete suite, including integration tests. It removes the
container afterward. No database URL or password needs to be copied manually.

See [agent evals](evals.md) for the separate, optional LLM evaluation workflow,
state graders, comparison reports, and Logfire integration.

For interactive MCP testing, use this command as a stdio server in your client
(with its working directory set to the repository):

```sh
uv run python -m devtools.postgres -- uv run python -m devtools.serve
```

It seeds a disposable encrypted tree with synthetic memories, including nested
projects and a long runbook. All changes disappear when the server exits. It
does not connect to production memory or require transport credentials. The
stdio round trip is also covered by the PostgreSQL integration suite.

## Unit tests

- Capability signature, issuer, audience, expiry, action, and delegation-depth enforcement
- Authorization before any store lookup
- Path normalization and traversal rejection
- AES-GCM tamper and moved-context rejection
- Local key rotation and restart durability
- Stable, non-disclosing public errors

## PostgreSQL integration tests

- Fresh-schema migration and migration from a built wheel
- Workspace and agent-profile isolation
- Platform-admin metadata boundaries and platform-only workspace creation
- Invite-only, all-authenticated, and injected external-entitlement admission
- Administrator-only and all-member agent creation
- Manager-without-content grants, transfers, and ownership changes
- Explicit administrator self-grants with the deployment flag enabled/disabled
- Atomic creator manager/full-access rows through API and MCP URL creation
- Migration without authorization backfill
- Encrypted directory traversal and Markdown round trips
- Concurrent compare-and-swap writes
- Concurrent idempotent retries and key-reuse rejection
- Immediate delete denial and bounded hard deletion
- Per-table janitor batch limits, repeated backlog draining, and per-object version ceilings
- Quota and rate-limit enforcement
- Raw-row scans for synthetic path and content canaries

These tests use PostgreSQL. SQLite is not an acceptable substitute for locking, constraints, JSON, or concurrency behavior.

## MCP and app tests

- Tool schemas contain no scope or capability-token arguments
- Clients without app support receive useful text and structured results
- MCP Apps resource metadata, MIME type, and network policy are correct
- UI-only backend helpers remain hidden from the model
- Two app instances surface compare-and-swap conflicts instead of overwriting
- Malicious Markdown cannot execute HTML or load arbitrary external resources

## Packaging tests

- Wheel and source archive contain migrations and UI resources
- The wheel installs into a clean environment
- Optional dependency groups import independently
- Source, tests, docs, and built artifacts contain no organization-specific names, email addresses, secrets, or proprietary fixtures
