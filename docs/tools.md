# Agent tool contract (0.5)

These tools act on the current capability's encrypted virtual tree, rooted at
`/`. They do not expose the host filesystem, execute a shell, or accept workspace
or principal arguments. Stored Markdown is data, never authorization or system
instructions.

| Tool | Use it for | Required capabilities |
| --- | --- | --- |
| `memory_list` | One directory's direct children and versions | list |
| `memory_glob` | Recursive filename discovery, without content reads | list |
| `memory_grep` | Matching content lines or paths containing matches | list + read |
| `memory_read` | Exact current text, paged by line/column, and current version | read |
| `memory_edit` | A small exact-text replacement preserving the rest | read + write |
| `memory_write` | Creating a document or intentionally replacing its entire content | write |
| `memory_append` | Adding exact text at the end, with caller-supplied separators | append |
| `memory_delete` | Deleting one document with immediate access denial | delete |
| `memory_history_list` | Retained version metadata, newest first | history:list |
| `memory_history_read` | Retained historical text and optional unified diff | history:read |

Capability names above have the `memory:` prefix. No new grants or database
migrations are needed. The server asks the resolver for read when searching and
write when editing, then checks the additional capability on that same verified
invocation before validating inputs or touching storage.

## Search, read, edit

```json
{"tool": "memory_grep", "arguments": {"pattern": "Retry limit", "path": "/ops"}}
{"tool": "memory_read", "arguments": {"path": "/ops/service.md", "start_line": 3, "max_lines": 5}}
{"tool": "memory_edit", "arguments": {"path": "/ops/service.md", "old_text": "Retry limit: 3", "new_text": "Retry limit: 5", "expected_version": 1, "idempotency_key": "retry-limit-1"}}
```

Use the version actually returned by read, not the example's `1`. Edits match
literal text, including whitespace and line endings. The default requires one
unique occurrence. Include surrounding text to disambiguate; `replace_all=true`
explicitly replaces all non-overlapping occurrences. Empty `new_text` deletes the
match. No match, ambiguous matches, identical old/new text, a stale version, or
quota overflow makes no change. Match checks, replacement, version update, and
idempotency recording occur under the same PostgreSQL transaction and scope lock.
Identical retries return the original result even after another version exists.

Use a new idempotency key for each new logical mutation. An identical retry keeps
all original arguments, including `expected_version`. After a version conflict,
read again, reconcile, and submit the revised request with a new key. An edit
requires both read and write; an append-only capability remains append-only.

## Bounds and continuation

`memory_read` defaults to 200 lines and caps each page at 20,000 characters.
`max_lines` can be raised to 2,000. `start_line` and `start_column` are 1-based;
follow **both** `next_start_line` and `next_start_column` while `truncated=true`.
This also permits lossless pagination through a single very long line. Content
has no inserted line-number prefixes. `total_lines` refers to the full document.
Pages are current reads, not a snapshot transaction: if their versions differ,
restart or reconcile them. Never use a partial read as the body of a full write.

List/glob/grep return `next_offset` (0-based) for additional result pages. Their
ordering is deterministic for an unchanged tree; concurrent mutations can move
entries between pages. Glob supports `*`, `?`, character classes, and whole `**`
segments (`**/*.md` includes root-level documents). Patterns are relative to a
directory `path`; no brace expansion, leading slash, or traversal segments.

Grep accepts a file or directory `path` and defaults to literal, case-sensitive,
single-line substring search. For a single file, glob matches its filename. Set
`literal=false` for a Python-compatible regular expression (the `regex` package),
or `case_sensitive=false` for case-insensitive matching. This is **not ripgrep's
regex dialect**. Content mode returns matching lines, versions, nearby context,
and 1-based snippet columns. Snippets are capped at 500 characters and marked
when clipped. `files_with_matches` returns one entry per matching document.

Each recursive call scans at most 1,000 manifest entries and 100 directories;
grep additionally caps scanning at 200 documents and 2 MiB of document content.
Search has a five-second scan deadline checked between reads/lines, and regex
matching has a 20 ms per-line execution timeout. Database/network latency remains
subject to the host's transport timeout. The final read may bring the reported
scanned byte count above the budget; that document is not searched. Result text
also has a 20,000-character budget (JSON metadata adds overhead).

`limit_reasons=["result_limit"]` means follow `next_offset`. `scan_limit` means
narrow the directory or glob; no continuation past the scan budget is promised.
Empty **incomplete** results do not establish absence. Search decrypts only within
the authorized invocation and never exports plaintext files or builds a plaintext
index. Narrow searches cost fewer reads and consume less of the existing scope
rate limit. History operations retain their existing bounded retention semantics.

## Upgrading from 0.4

Version 0.5 is an alpha minor release with a deliberate contract change:
`memory_read` and `memory_list` are now paged by default. Existing names and
mutation parameter names remain; callers must handle continuation. MCP App UI
helpers keep their full-document payloads and are still hidden from models.
Embedded adapters implementing `MemoryStore` must add the atomic `edit` method;
the built-in PostgreSQL adapter implements it. Do not implement edit as an
unlocked read followed by write: retries and concurrent updates would differ.

We retained the `memory_` namespace to avoid collisions with local file tools.
`glob`, `grep`, `read`, and `edit` follow familiar agent operations while the
descriptions make virtual paths, pagination, exact matching, and retry recovery
explicit. Claude Code documents [Grep backed by ripgrep and targeted Edit](https://code.claude.com/docs/en/tools-reference);
OpenAI documents [focused patches plus shell discovery](https://developers.openai.com/api/docs/guides/tools-apply-patch).
The interface borrows those affordances; it does not copy their shell execution
or filesystem storage model. See [evals](evals.md) for measuring the tradeoffs.
