"""Serve the version-matched management frontend from the Python package."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def _relative_application_url(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{field} must be a non-empty relative URL")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError(f"{field} must be a same-origin relative URL")
    if value.startswith("//") or not value.startswith(("/", "./", "../")):
        raise ValueError(f"{field} must be a same-origin relative URL")
    return value


def _https_authority(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("OIDC authority must be an HTTPS origin or issuer URL")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class FrontendAuthConfig:
    """Browser authentication mode; all values are public client metadata."""

    mode: Literal["none", "session", "oidc"]
    authority: str | None = None
    client_id: str | None = None
    scope: str = "openid profile email"
    token_field: Literal["id_token", "access_token"] = "access_token"
    auto_login: bool = False

    def __post_init__(self) -> None:
        if self.mode == "oidc":
            if not self.authority or not self.client_id:
                raise ValueError("OIDC mode requires authority and client_id")
            object.__setattr__(self, "authority", _https_authority(self.authority))
            if not re.fullmatch(r"[A-Za-z0-9._~-]{1,255}", self.client_id):
                raise ValueError("OIDC client_id is invalid")
            scopes = self.scope.split()
            if "openid" not in scopes or any(
                re.fullmatch(r"[^\s]{1,255}", item) is None for item in scopes
            ):
                raise ValueError("OIDC scope must contain openid")
        elif self.authority is not None or self.client_id is not None:
            raise ValueError("authority and client_id require OIDC mode")


@dataclass(frozen=True, slots=True)
class ManagementFrontendConfig:
    """Same-origin UI routing and authentication configuration."""

    api_base_url: str = "../api"
    auth: FrontendAuthConfig = FrontendAuthConfig(mode="session")
    product_name: str = "Agent Filetree Memory"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "api_base_url",
            _relative_application_url(
                self.api_base_url,
                field="api_base_url",
            ),
        )
        if (
            not isinstance(self.product_name, str)
            or not self.product_name.strip()
            or len(self.product_name) > 80
            or "\x00" in self.product_name
        ):
            raise ValueError("product_name is invalid")


class _SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, oidc_authority: str | None) -> None:
        self._app = app
        connect_sources = ["'self'"]
        if oidc_authority:
            parsed = urlparse(oidc_authority)
            connect_sources.append(f"{parsed.scheme}://{parsed.netloc}")
        self._csp = (
            "default-src 'none'; "
            "base-uri 'self'; "
            f"connect-src {' '.join(connect_sources)}; "
            "font-src 'self'; frame-ancestors 'none'; img-src 'self' data:; "
            "manifest-src 'self'; script-src 'self'; style-src 'self'"
        ).encode("ascii")

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    (
                        (b"content-security-policy", self._csp),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                    )
                )
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _bundled_dist() -> Path:
    return Path(str(files(__package__).joinpath("dist")))


def create_management_frontend(
    config: ManagementFrontendConfig | None = None,
    *,
    static_directory: str | Path | None = None,
    check_dir: bool = True,
) -> ASGIApp:
    """Create the standalone SPA app for mounting at a host-selected path."""

    resolved = config or ManagementFrontendConfig()
    directory = Path(static_directory) if static_directory else _bundled_dist()
    app = FastAPI(
        title=f"{resolved.product_name} UI",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/config.json", include_in_schema=False)
    async def frontend_config() -> JSONResponse:
        return JSONResponse(
            {
                "api_base_url": resolved.api_base_url,
                "auth": asdict(resolved.auth),
                "product_name": resolved.product_name,
            },
            headers={"Cache-Control": "no-store"},
        )

    app.frontend(
        "/",
        directory=str(directory),
        fallback="index.html",
        check_dir=check_dir,
    )
    return _SecurityHeadersMiddleware(
        app,
        oidc_authority=resolved.auth.authority,
    )
