# Security

## Supported versions

This project is pre-release. Only the current development version receives security fixes.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Contact the repository owner privately and include the affected version, reproduction steps, impact, and any suggested mitigation. Avoid including real memory content, credentials, signing keys, or database dumps.

## Security model

The trusted host is responsible for authenticating callers and issuing a short-lived capability. The service verifies that capability before any lookup or decryption. A capability binds the outer authenticated principal and invocation to one workspace, immutable agent profile, action set, expiry, and delegation depth. The verifier requires the principal in the signed capability to match the principal established by transport authentication. The principal is the current actor, not part of the durable memory key; the host must issue access only to workspace members authorized for that agent profile.

The database is not a plaintext content index. Paths and directory manifests are encrypted alongside document bodies. Each immutable version uses a fresh AES-256-GCM data-encryption key. A configurable provider wraps that key with the same opaque encryption context used as authenticated data for the document ciphertext.

The design protects against database-only disclosure, accidental cross-scope lookup, moved ciphertext, stale writes, replayed writes, and disclosure through routine audit fields. It does not claim to protect plaintext from a compromised live service process that is authorized to decrypt it.

Deployers remain responsible for network authentication, signing-key custody, encryption-key custody, PostgreSQL access control, backups, observability policy, denial-of-service protection, invoking the packaged retention janitor, and secure deletion guarantees in their storage infrastructure. The request-serving runtime does not contain a scheduler.
