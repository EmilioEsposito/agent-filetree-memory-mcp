"""Compose the management API, MCP transport, and bundled UI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount
from starlette.types import ASGIApp

from .frontend import ManagementFrontendConfig, create_management_frontend


def _mount_path(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*", value)
        is None
    ):
        raise ValueError(f"{field} must be a non-root absolute URL path")
    return value


def create_web_application(
    *,
    management_api: ASGIApp,
    mcp: Any,
    frontend_config: ManagementFrontendConfig | None = None,
    api_path: str = "/api",
    mcp_path: str = "/mcp",
    ui_path: str = "/ui",
    mcp_http_options: Mapping[str, Any] | None = None,
    extra_routes: Sequence[BaseRoute] = (),
) -> Starlette:
    """Return one app serving version-matched API, MCP, and UI surfaces.

    Authentication remains adapter-owned: ``management_api`` contains the
    deployer's principal dependency and ``mcp`` contains its transport auth and
    invocation resolver. The returned app can itself be mounted below any host
    route prefix.
    """

    paths = {
        _mount_path(api_path, field="api_path"),
        _mount_path(mcp_path, field="mcp_path"),
        _mount_path(ui_path, field="ui_path"),
    }
    if len(paths) != 3:
        raise ValueError("api_path, mcp_path, and ui_path must be distinct")
    options = dict(mcp_http_options or {})
    options.setdefault("stateless_http", True)
    options.setdefault("json_response", True)
    inner_mcp = mcp.http_app(path=mcp_path, **options)
    frontend = create_management_frontend(frontend_config)
    return Starlette(
        routes=[
            *extra_routes,
            Mount(api_path, app=management_api),
            Mount(ui_path, app=frontend),
            Mount("/", app=inner_mcp),
        ],
        lifespan=inner_mcp.lifespan,
    )
