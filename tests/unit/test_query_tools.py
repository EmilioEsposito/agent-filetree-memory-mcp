from datetime import datetime, timedelta, timezone

import pytest

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.application import queries
from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import (
    DocumentSnapshot,
    MemoryAction,
    MemoryEntry,
    Scope,
    VerifiedInvocation,
)


def invocation(*actions):
    now = datetime.now(timezone.utc)
    return VerifiedInvocation(
        Scope("test", "agent"),
        "principal",
        "invocation",
        "capability",
        "issuer",
        "audience",
        frozenset(actions),
        now,
        now + timedelta(hours=1),
    )


class QueryStore:
    def __init__(self, files):
        self.files = files
        self.calls = []

    async def list(self, scope, path, **kwargs):
        self.calls.append(("list", path))
        prefix = path.rstrip("/") + "/"
        entries = {}
        for name in self.files:
            if name.startswith(prefix):
                tail = name[len(prefix) :]
                child = tail.split("/")[0]
                entries[child] = MemoryEntry(
                    child,
                    prefix + child,
                    "directory" if "/" in tail else "document",
                    1,
                    datetime.now(timezone.utc),
                )
        return list(entries.values())

    async def read(self, scope, path, **kwargs):
        self.calls.append(("read", path))
        now = datetime.now(timezone.utc)
        return DocumentSnapshot(path, self.files[path], 1, now, now)


async def test_glob_pagination_and_directory_scope_without_content_reads():
    store = QueryStore({"/b.md": "x", "/a.md": "x", "/nested/c.md": "x"})
    service, inv = MemoryService(store), invocation(MemoryAction.LIST)
    page = await queries.glob_documents(service, inv, "**/*.md", limit=1)
    assert page["paths"] == ["/a.md"] and page["next_offset"] == 1
    page = await queries.glob_documents(service, inv, "**/*.md", offset=1)
    assert page["paths"] == ["/b.md", "/nested/c.md"] and not page["truncated"]
    assert all(action == "list" for action, _ in store.calls)
    assert (await queries.glob_documents(service, inv, "*.md", path="/nested"))[
        "paths"
    ] == ["/nested/c.md"]


async def test_grep_literal_regex_modes_context_and_pagination():
    store = QueryStore(
        {"/a.md": "before\n[a-z]+\nUPPER\n[a-z]+\nafter\n", "/b.md": "UPPER"}
    )
    service = MemoryService(store)
    inv = invocation(MemoryAction.LIST, MemoryAction.READ)
    page = await queries.grep_documents(service, inv, "[a-z]+", limit=1)
    match = page["matches"][0]
    assert match["line_number"] == 2 and match["text"] == "[a-z]+"
    assert [line["line_number"] for line in match["context"]] == [1, 3]
    assert page["next_offset"] == 1
    page = await queries.grep_documents(service, inv, "[a-z]+", offset=1)
    assert [m["line_number"] for m in page["matches"]] == [4]
    page = await queries.grep_documents(
        service,
        inv,
        "^upper$",
        literal=False,
        case_sensitive=False,
        output_mode="files_with_matches",
    )
    assert [m["path"] for m in page["matches"]] == ["/a.md", "/b.md"]
    assert all("text" not in m for m in page["matches"])


async def test_grep_snippet_centers_on_match_in_long_line():
    service = MemoryService(QueryStore({"/a.md": "x" * 30000 + "needle" + "y" * 30000}))
    page = await queries.grep_documents(
        service, invocation(MemoryAction.LIST, MemoryAction.READ), "needle"
    )
    match = page["matches"][0]
    assert "needle" in match["text"] and len(match["text"]) <= 500
    assert match["truncated"] and match["start_column"] > 1


async def test_scan_exhaustion_is_distinct_from_complete_no_matches(monkeypatch):
    monkeypatch.setattr(queries, "MAX_SCAN_DOCUMENTS", 1)
    service = MemoryService(QueryStore({"/a.md": "none", "/b.md": "needle"}))
    page = await queries.grep_documents(
        service, invocation(MemoryAction.LIST, MemoryAction.READ), "needle"
    )
    assert page["matches"] == [] and page["truncated"]
    assert page["limit_reasons"] == ["scan_limit"] and page["next_offset"] is None


async def test_authorization_precedes_all_search_and_edit_validation():
    store = QueryStore({})
    service = MemoryService(store)
    with pytest.raises(AuthorizationDenied):
        await queries.grep_documents(
            service, invocation(MemoryAction.READ), {"private": "value"}
        )
    with pytest.raises(AuthorizationDenied):
        await queries.glob_documents(service, invocation(MemoryAction.WRITE), None)
    with pytest.raises(AuthorizationDenied):
        await service.edit(
            invocation(MemoryAction.WRITE),
            None,
            None,
            None,
            expected_version=1,
            idempotency_key="edit",
        )
    assert store.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"literal": "false"},
        {"offset": True},
        {"context_lines": 4},
        {"glob": "../*"},
        {"glob": "{a,b}.md"},
        {"output_mode": []},
    ],
)
async def test_invalid_search_arguments_never_access_store(kwargs):
    store = QueryStore({})
    with pytest.raises(ValueError):
        await queries.grep_documents(
            MemoryService(store),
            invocation(MemoryAction.LIST, MemoryAction.READ),
            "x",
            **kwargs,
        )
    assert store.calls == []


async def test_invalid_and_expensive_regex_fail_without_echoing_pattern():
    service = MemoryService(QueryStore({"/a.md": "a" * 20000 + "!"}))
    inv = invocation(MemoryAction.LIST, MemoryAction.READ)
    for pattern, message in (
        ("PRIVATE[", "invalid regular expression"),
        ("(a|aa)+$", "time limit"),
    ):
        with pytest.raises(ValueError, match=message) as exc:
            await queries.grep_documents(service, inv, pattern, literal=False)
        assert pattern not in str(exc.value)
