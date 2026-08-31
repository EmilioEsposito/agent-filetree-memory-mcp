from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from agent_filetree_memory.web import (
    FrontendAuthConfig,
    ManagementFrontendConfig,
    create_management_frontend,
)


def test_packaged_frontend_serves_config_html_and_safe_spa_fallback() -> None:
    app = create_management_frontend(
        ManagementFrontendConfig(
            api_base_url="../api/manage",
            auth=FrontendAuthConfig(mode="session"),
        )
    )
    with TestClient(app) as client:
        root = client.get("/", headers={"Accept": "text/html"})
        deep_link = client.get(
            "/workspace/example", headers={"Accept": "text/html"}
        )
        missing_asset = client.get("/assets/does-not-exist.js")
        config = client.get("/config.json")

    assert root.status_code == 200
    assert deep_link.status_code == 200
    assert "<div id=\"root\"></div>" in root.text
    assert missing_asset.status_code == 404
    assert missing_asset.headers["content-type"].startswith("application/json")
    assert config.json()["api_base_url"] == "../api/manage"
    assert config.json()["mcp_base_url"] == "../mcp"
    assert config.json()["auth"]["mode"] == "session"
    assert config.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in root.headers["content-security-policy"]
    assert root.headers["x-frame-options"] == "DENY"


def test_oidc_config_accepts_public_metadata_without_secrets() -> None:
    auth = FrontendAuthConfig(
        mode="oidc",
        authority="https://identity.example.test/tenant/",
        client_id="public-client-id",
        scope="openid profile email",
        token_field="id_token",
        auto_login=True,
    )
    app = create_management_frontend(
        ManagementFrontendConfig(auth=auth)
    )
    with TestClient(app) as client:
        payload = client.get("/config.json").json()

    assert payload["auth"] == {
        "mode": "oidc",
        "authority": "https://identity.example.test/tenant",
        "client_id": "public-client-id",
        "scope": "openid profile email",
        "token_field": "id_token",
        "auto_login": True,
    }
    assert "secret" not in str(payload).lower()


@pytest.mark.parametrize(
    "value",
    ["https://other.example.test/api", "//other.example.test/api", "api"],
)
def test_api_base_must_stay_same_origin(value: str) -> None:
    with pytest.raises(ValueError, match="same-origin relative URL"):
        ManagementFrontendConfig(api_base_url=value)


@pytest.mark.parametrize(
    "value",
    ["https://other.example.test/mcp", "//other.example.test/mcp", "mcp"],
)
def test_mcp_base_must_stay_same_origin(value: str) -> None:
    with pytest.raises(ValueError, match="same-origin relative URL"):
        ManagementFrontendConfig(mcp_base_url=value)


def test_bundled_distribution_is_real_not_a_placeholder() -> None:
    dist = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "agent_filetree_memory"
        / "web"
        / "dist"
    )
    html = (dist / "index.html").read_text(encoding="utf-8")
    assets = list((dist / "assets").glob("*.js"))
    assert "<div id=\"root\"></div>" in html
    assert assets and all(item.stat().st_size > 10_000 for item in assets)
