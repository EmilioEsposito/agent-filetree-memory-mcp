"""Local visual preview of the real Prefab MCP App component tree.

Run from the repository root with:

    uv run --locked prefab serve examples/mcp_app_preview.py:preview_app --reload

The preview exercises client-side interactions such as Add file, Edit, and
Cancel. MCP tool calls still require a real host, so save/list/read actions are
not expected to complete in this standalone renderer.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastmcp import Client
from prefab_ui.app import PrefabApp

from agent_filetree_memory.domain.models import (
    MemoryAction,
    Scope,
    VerifiedInvocation,
)
from agent_filetree_memory.mcp import create_mcp_server


_NOW = datetime.now(timezone.utc)
_CONTENT = """# Project memory

This is a local preview of the same **Prefab UI** shipped in the MCP App.

| Topic | Decision |
| --- | --- |
| Default mode | Rendered Markdown |
| Editing | Explicit Edit button |
"""


class _PreviewResolver:
    async def __call__(
        self,
        _ctx: Any,
        action: MemoryAction,
    ) -> VerifiedInvocation:
        return VerifiedInvocation(
            scope=Scope(
                workspace_id="local-preview-workspace",
                agent_profile_id="local-preview-agent",
            ),
            principal_id="local-preview-user",
            invocation_id="local-preview-invocation",
            capability_id="local-preview-capability",
            issuer="local-preview",
            audience="local-preview",
            allowed_actions=frozenset({action}),
            issued_at=_NOW,
            expires_at=_NOW + timedelta(hours=1),
        )


async def _build_preview() -> PrefabApp:
    # memory_browse resolves identity but does not read memory until its private
    # app-only tools run. A placeholder is therefore sufficient for this visual
    # preview and cannot accidentally touch a real store.
    server = create_mcp_server(
        object(),  # type: ignore[arg-type]
        _PreviewResolver(),
        include_app=True,
    )
    async with Client(server) as client:
        result = await client.call_tool("memory_browse", {})

    payload = result.structured_content
    if not isinstance(payload, dict):
        raise RuntimeError("memory_browse did not return a Prefab payload")
    payload["state"].update(
        {
            "listing": {
                "path": "/projects",
                "parent_path": "/",
                "folder_input": "projects",
                "directories": [],
                "documents": [
                    {
                        "name": "memory.md",
                        "path": "/projects/memory.md",
                        "kind": "document",
                        "version": 3,
                        "version_created_at": _NOW.isoformat(),
                    }
                ],
            },
            "selected": {
                "path": "/projects/memory.md",
                "content": _CONTENT,
                "version": 3,
            },
            "draft_path": "/projects/memory.md",
            "draft_content": _CONTENT,
            "current_version": 3,
            "loading": False,
        }
    )
    _remove_on_mount(payload)
    return PrefabApp.model_validate(payload)


def _remove_on_mount(value: Any) -> None:
    """Disable host-only initial tool calls in the standalone renderer."""
    if isinstance(value, dict):
        value.pop("onMount", None)
        for child in value.values():
            _remove_on_mount(child)
    elif isinstance(value, list):
        for child in value:
            _remove_on_mount(child)


preview_app = asyncio.run(_build_preview())
