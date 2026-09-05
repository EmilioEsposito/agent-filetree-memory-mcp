from __future__ import annotations

import json
import logging
import re

import pytest
from fastmcp import Client

from agent_filetree_memory.application import MemoryService
from agent_filetree_memory.domain.errors import AuthorizationDenied
from agent_filetree_memory.domain.models import MemoryAction
from agent_filetree_memory.mcp import create_mcp_server

PRIVATE_PATH = "/private/canary.md"
PRIVATE_CONTENT = "# PRIVATE-CONTENT-CANARY\n\nOnly the verified agent can read this."


SCOPE_ARGUMENTS = {
    "workspace",
    "workspace_id",
    "agent",
    "agent_id",
    "agent_profile_id",
    "capability",
    "capability_token",
}


async def test_headless_protocol_exposes_ten_bounded_tools(service, resolver):
    server = create_mcp_server(service, resolver, include_app=False)

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {
        "memory_glob",
        "memory_grep",
        "memory_edit",
        "memory_list",
        "memory_read",
        "memory_history_list",
        "memory_history_read",
        "memory_write",
        "memory_append",
        "memory_delete",
    }
    for tool in tools.values():
        assert not (set(tool.inputSchema["properties"]) & SCOPE_ARGUMENTS)
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.idempotentHint is True

    assert tools["memory_list"].annotations.readOnlyHint is True
    assert tools["memory_read"].annotations.readOnlyHint is True
    assert tools["memory_history_list"].annotations.readOnlyHint is True
    assert tools["memory_history_read"].annotations.readOnlyHint is True
    assert tools["memory_write"].annotations.destructiveHint is True
    assert tools["memory_append"].annotations.destructiveHint is False
    assert tools["memory_delete"].annotations.destructiveHint is True


async def test_mutation_schemas_encode_create_only_cas_and_safe_retries(
    service, resolver
):
    server = create_mcp_server(service, resolver, include_app=False)

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    write = tools["memory_write"].inputSchema
    assert "expected_version" not in write["required"]
    assert write["properties"]["expected_version"]["default"] is None
    assert write["properties"]["idempotency_key"]["minLength"] == 1
    assert write["properties"]["co_authored_by"]["maxItems"] == 8
    assert "change_comment" not in write["required"]

    for name in ("memory_append", "memory_delete"):
        schema = tools[name].inputSchema
        assert "expected_version" in schema["required"]
        assert schema["properties"]["expected_version"]["minimum"] == 1
        assert "idempotency_key" in schema["required"]


@pytest.mark.parametrize(
    "name", ["memory_glob", "memory_grep", "memory_edit", "memory_read"]
)
async def test_new_tool_malformed_or_missing_arguments_authorize_before_validation(
    service, caplog, name
):
    async def deny(ctx, action):
        raise AuthorizationDenied("memory operation is not authorized")

    marker = "PRIVATE-NEW-TOOL-INPUT"
    server = create_mcp_server(service, deny)
    async with Client(server) as client:
        result = await client.call_tool(
            name, {"path": {"private": marker}}, raise_on_error=False
        )
    assert result.is_error
    assert "not authorized" in result.content[0].text
    assert marker not in repr(result) and marker not in caplog.text
    assert service.calls == []


async def test_protocol_calls_resolve_action_and_serialize_domain_results(
    service, resolver
):
    server = create_mcp_server(service, resolver, include_app=False)

    async with Client(server) as client:
        listed = await client.call_tool("memory_list", {"path": "/private"})
        read = await client.call_tool("memory_read", {"path": PRIVATE_PATH})
        history = await client.call_tool(
            "memory_history_list",
            {"path": PRIVATE_PATH, "limit": 10},
        )
        historical = await client.call_tool(
            "memory_history_read",
            {
                "path": PRIVATE_PATH,
                "version": 6,
                "compare_to_version": 7,
            },
        )
        written = await client.call_tool(
            "memory_write",
            {
                "path": PRIVATE_PATH,
                "content": "# replaced",
                "expected_version": 7,
                "idempotency_key": "write-1",
                "co_authored_by": ["agent:claude"],
                "change_comment": "Replace heading",
            },
        )
        appended = await client.call_tool(
            "memory_append",
            {
                "path": PRIVATE_PATH,
                "content": "\nnext",
                "expected_version": 8,
                "idempotency_key": "append-1",
                "change_comment": "Add next item",
            },
        )
        deleted = await client.call_tool(
            "memory_delete",
            {
                "path": PRIVATE_PATH,
                "expected_version": 9,
                "idempotency_key": "delete-1",
            },
        )

    assert listed.structured_content["entries"][0]["path"] == PRIVATE_PATH
    assert listed.structured_content["entries"][0]["version_created_at"].startswith(
        "2026-08-28T16:00:00"
    )
    assert read.structured_content["content"] == PRIVATE_CONTENT
    assert (
        read.structured_content["version_created_at"]
        == read.structured_content["updated_at"]
    )
    assert read.structured_content["committed_by"] == {
        "principal_id": "principal-secret",
        "verification": "authenticated",
    }
    assert read.structured_content["co_authored_by"] == [
        {"identifier": "agent:claude", "verification": "self_asserted"}
    ]
    assert read.structured_content["change_comment"] == "Seed current version"
    assert [item["version"] for item in history.structured_content["versions"]] == [
        7,
        6,
    ]
    assert history.structured_content["versions"][0]["change_comment"] == (
        "Seed current version"
    )
    assert history.structured_content["versions"][1]["committed_by"] == {
        "principal_id": "principal-previous",
        "verification": "authenticated",
    }
    assert historical.structured_content["content"] == "# previous"
    assert historical.structured_content["change_comment"] == "Previous version"
    assert historical.structured_content["compared_to_version"] == 7
    assert historical.structured_content["diff"].startswith("--- old")
    assert written.structured_content == {
        "path": PRIVATE_PATH,
        "version": 8,
        "created": False,
        "idempotent_replay": False,
    }
    assert appended.structured_content["version"] == 9
    assert deleted.structured_content["deleted_version"] == 9
    assert deleted.structured_content["purge_after"].startswith("2026-09-27T16:00:00")
    assert [action for _, action in resolver.calls] == [
        MemoryAction.LIST,
        MemoryAction.READ,
        MemoryAction.HISTORY_LIST,
        MemoryAction.HISTORY_READ,
        MemoryAction.WRITE,
        MemoryAction.APPEND,
        MemoryAction.DELETE,
    ]
    assert all(call[1] is resolver.invocation for call in service.calls)
    assert service.calls[4][2]["co_authored_by"] == ["agent:claude"]
    assert service.calls[4][2]["change_comment"] == "Replace heading"
    assert service.calls[5][2]["change_comment"] == "Add next item"


async def test_history_capabilities_are_independent(service, resolver):
    resolver.action_scoped = True
    server = create_mcp_server(service, resolver, include_app=False)

    async with Client(server) as client:
        await client.call_tool(
            "memory_history_list",
            {"path": PRIVATE_PATH},
        )
        await client.call_tool(
            "memory_history_read",
            {"path": PRIVATE_PATH, "version": 6},
        )

    assert [action for _, action in resolver.calls] == [
        MemoryAction.HISTORY_LIST,
        MemoryAction.HISTORY_READ,
    ]
    assert service.calls[0][1].allowed_actions == frozenset({MemoryAction.HISTORY_LIST})
    assert service.calls[1][1].allowed_actions == frozenset({MemoryAction.HISTORY_READ})


async def test_create_without_expected_version_reaches_service_unchanged(
    service, resolver
):
    server = create_mcp_server(service, resolver, include_app=False)

    async with Client(server) as client:
        result = await client.call_tool(
            "memory_write",
            {
                "path": "/new.md",
                "content": "# New",
                "idempotency_key": "create-1",
            },
        )

    assert result.structured_content["created"] is True
    assert service.calls[-1][2]["expected_version"] is None


async def test_authorization_precedes_rejected_argument_validation_without_echo(
    service, caplog
):
    class DenyingResolver:
        def __init__(self) -> None:
            self.actions: list[MemoryAction] = []

        async def __call__(self, _ctx, action: MemoryAction):
            self.actions.append(action)
            raise AuthorizationDenied("memory operation is not authorized")

    resolver = DenyingResolver()
    server = create_mcp_server(service, resolver, include_app=False)
    workspace_canary = "PRIVATE-WORKSPACE-ARGUMENT-CANARY"
    path_canary = "PRIVATE-PATH-REJECTED-CANARY"
    content_canary = "PRIVATE-CONTENT-REJECTED-CANARY"
    caplog.set_level(logging.WARNING)

    async with Client(server) as client:
        result = await client.call_tool(
            "memory_write",
            {
                # These values violate the advertised schemas. Runtime typing is
                # deliberately permissive so trusted authorization runs first.
                "path": {"value": path_canary},
                "content": {"value": content_canary},
                "idempotency_key": "rejected-input-1",
                "workspace_id": workspace_canary,
            },
            raise_on_error=False,
        )

    rendered_result = repr(result)
    assert result.is_error is True
    assert resolver.actions == [MemoryAction.WRITE]
    assert service.calls == []
    assert "memory operation is not authorized" in result.content[0].text
    assert workspace_canary not in rendered_result
    assert path_canary not in rendered_result
    assert content_canary not in rendered_result
    assert workspace_canary not in caplog.text
    assert path_canary not in caplog.text
    assert content_canary not in caplog.text


@pytest.mark.parametrize("invalid_field", ["path", "content"])
async def test_authorized_rejected_values_are_not_echoed_or_persisted(
    resolver, caplog, invalid_field
):
    class NeverStore:
        called = False

        async def write(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("invalid values must not reach persistence")

    store = NeverStore()
    server = create_mcp_server(MemoryService(store), resolver, include_app=False)
    path_canary = "AUTHORIZED-INVALID-PATH-CANARY"
    content_canary = "AUTHORIZED-INVALID-CONTENT-CANARY"
    arguments: dict[str, object] = {
        "path": "/valid.md",
        "content": "valid",
        "idempotency_key": "authorized-rejected-1",
    }
    arguments[invalid_field] = {
        "value": path_canary if invalid_field == "path" else content_canary
    }
    caplog.set_level(logging.WARNING)

    async with Client(server) as client:
        result = await client.call_tool(
            "memory_write",
            arguments,
            raise_on_error=False,
        )

    rendered_result = repr(result)
    assert result.is_error is True
    assert [action for _, action in resolver.calls] == [MemoryAction.WRITE]
    assert store.called is False
    assert path_canary not in rendered_result
    assert content_canary not in rendered_result
    assert path_canary not in caplog.text
    assert content_canary not in caplog.text


async def test_authorized_unexpected_argument_is_rejected_without_echo(
    resolver, caplog
):
    class NeverStore:
        called = False

        async def list(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("invalid values must not reach persistence")

    store = NeverStore()
    server = create_mcp_server(MemoryService(store), resolver, include_app=False)
    canary = "PRIVATE-CANARY"
    caplog.set_level(logging.WARNING)

    async with Client(server) as client:
        result = await client.call_tool(
            "memory_list",
            {"path": "/", "workspace_id": canary},
            raise_on_error=False,
        )

    rendered_result = repr(result)
    assert result.is_error is True
    assert [action for _, action in resolver.calls] == [MemoryAction.LIST]
    assert store.called is False
    assert "invalid memory path" in result.content[0].text
    assert canary not in rendered_result
    assert canary not in caplog.text


async def test_app_helper_unexpected_argument_uses_same_safe_boundary(resolver, caplog):
    class NeverStore:
        called = False

        async def list(self, *_args, **_kwargs):
            self.called = True
            raise AssertionError("invalid values must not reach persistence")

    store = NeverStore()
    server = create_mcp_server(MemoryService(store), resolver, include_app=True)
    canary = "PRIVATE-APP-ARGUMENT-CANARY"
    caplog.set_level(logging.WARNING)

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        serialized = json.dumps(opened.structured_content)
        helper_match = re.search(r"[0-9a-f]{12}_ui_memory_list", serialized)
        instance_matches = set(
            re.findall(r'"app_instance_id"\s*:\s*"([A-Za-z0-9_-]+)"', serialized)
        )
        assert helper_match is not None
        assert len(instance_matches) == 1
        result = await client.call_tool(
            helper_match.group(0),
            {
                "app_instance_id": instance_matches.pop(),
                "path": "/",
                "workspace_id": canary,
            },
            raise_on_error=False,
        )

    rendered_result = repr(result)
    assert result.is_error is True
    assert [action for _, action in resolver.calls] == [
        MemoryAction.LIST,
        MemoryAction.LIST,
    ]
    assert store.called is False
    assert "invalid memory path" in result.content[0].text
    assert canary not in rendered_result
    assert canary not in caplog.text


async def test_memory_browse_unexpected_argument_returns_fixed_safe_error(
    service, resolver, caplog
):
    server = create_mcp_server(service, resolver, include_app=True)
    canary = "PRIVATE-BROWSE-ARGUMENT-CANARY"
    caplog.set_level(logging.WARNING)

    async with Client(server) as client:
        result = await client.call_tool(
            "memory_browse",
            {"workspace_id": canary},
            raise_on_error=False,
        )

    rendered_result = repr(result)
    assert result.is_error is True
    assert [action for _, action in resolver.calls] == [MemoryAction.LIST]
    assert service.calls == []
    assert "invalid memory tool arguments" in result.content[0].text
    assert canary not in rendered_result
    assert canary not in caplog.text
