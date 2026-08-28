"""Fail-closed standalone stdio launcher for the packaged MCP server."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any

from .domain.errors import ConfigurationError
from .domain.models import MemoryAction, VerifiedInvocation


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"required standalone setting is missing: {name}")
    return value


def _boolean(
    environ: Mapping[str, str], name: str, *, default: bool
) -> bool:
    value = environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _base64_secret(value: str, *, name: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded, validate=True)
        if len(decoded) != 32 or base64.b64encode(decoded) != encoded:
            raise ValueError
        return decoded
    except (TypeError, UnicodeError, ValueError):
        raise ConfigurationError(f"{name} must encode exactly 32 bytes") from None


@dataclass(frozen=True, slots=True)
class StandaloneSettings:
    """Explicit standalone configuration with secrets suppressed from repr."""

    database_url: str = field(repr=False)
    schema: str
    keyring: Mapping[str, str] = field(repr=False)
    active_key_id: str
    idempotency_index_key: bytes = field(repr=False)
    capability_token: str = field(repr=False)
    capability_public_key_file: Path = field(repr=False)
    capability_key_id: str
    capability_issuer: str
    capability_audience: str
    principal_id: str
    service_namespace: str = "agent-filetree-memory"
    include_app: bool = False

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> "StandaloneSettings":
        values = os.environ if environ is None else environ
        raw_keyring = _required(values, "AGENT_FILETREE_MEMORY_KEYRING_JSON")
        try:
            keyring = json.loads(raw_keyring)
        except json.JSONDecodeError:
            raise ConfigurationError(
                "AGENT_FILETREE_MEMORY_KEYRING_JSON must be a JSON object"
            ) from None
        if not isinstance(keyring, dict) or not keyring or any(
            not isinstance(key_id, str) or not isinstance(key, str)
            for key_id, key in keyring.items()
        ):
            raise ConfigurationError(
                "AGENT_FILETREE_MEMORY_KEYRING_JSON must be a JSON object"
            )

        return cls(
            database_url=_required(values, "DATABASE_URL"),
            schema=values.get(
                "AGENT_FILETREE_MEMORY_DATABASE_SCHEMA", "agent_filetree_memory"
            ),
            keyring=keyring,
            active_key_id=_required(
                values, "AGENT_FILETREE_MEMORY_ACTIVE_KEY_ID"
            ),
            idempotency_index_key=_base64_secret(
                _required(values, "AGENT_FILETREE_MEMORY_IDEMPOTENCY_INDEX_KEY"),
                name="AGENT_FILETREE_MEMORY_IDEMPOTENCY_INDEX_KEY",
            ),
            capability_token=_required(
                values, "AGENT_FILETREE_MEMORY_CAPABILITY_TOKEN"
            ),
            capability_public_key_file=Path(
                _required(
                    values,
                    "AGENT_FILETREE_MEMORY_CAPABILITY_PUBLIC_KEY_FILE",
                )
            ).expanduser(),
            capability_key_id=_required(
                values, "AGENT_FILETREE_MEMORY_CAPABILITY_KEY_ID"
            ),
            capability_issuer=_required(
                values, "AGENT_FILETREE_MEMORY_CAPABILITY_ISSUER"
            ),
            capability_audience=_required(
                values, "AGENT_FILETREE_MEMORY_CAPABILITY_AUDIENCE"
            ),
            principal_id=_required(
                values, "AGENT_FILETREE_MEMORY_PRINCIPAL_ID"
            ),
            service_namespace=values.get(
                "AGENT_FILETREE_MEMORY_SERVICE_NAMESPACE",
                "agent-filetree-memory",
            ),
            include_app=_boolean(
                values,
                "AGENT_FILETREE_MEMORY_ENABLE_APP",
                default=False,
            ),
        )


class StaticCapabilityResolver:
    """Verify one explicit pre-issued capability for every requested action."""

    def __init__(
        self,
        verifier: Any,
        *,
        token: str,
        principal_id: str,
    ) -> None:
        self._verifier = verifier
        self._token = token
        self._principal_id = principal_id

    async def __call__(
        self, _ctx: Any, action: MemoryAction
    ) -> VerifiedInvocation:
        return self._verifier.verify(
            self._token,
            required_action=action,
            expected_principal_id=self._principal_id,
        )


@dataclass(slots=True)
class StandaloneRuntime:
    """Constructed server and its explicitly owned database runtime."""

    server: Any
    postgres: Any

    async def close(self) -> None:
        await self.postgres.close()


def _read_public_key(path: Path) -> bytes:
    try:
        value = path.read_bytes()
    except OSError:
        raise ConfigurationError("capability public key file is unavailable") from None
    if not value or len(value) > 16 * 1024:
        raise ConfigurationError("capability public key file is invalid")
    return value


def create_standalone_runtime(
    settings: StandaloneSettings | None = None,
) -> StandaloneRuntime:
    """Construct the standalone server without connecting or running DDL."""
    try:
        from .application import MemoryService
        from .auth import AsymmetricCapabilityVerifier
        from .crypto import EnvelopeEncryptor, LocalKeyringDekProvider
        from .mcp import create_mcp_server
        from .postgres import (
            PostgresMemoryStore,
            PostgresRuntime,
            PostgresStoreConfig,
        )
    except ImportError as exc:
        raise ConfigurationError(
            "standalone launcher requires agent-filetree-memory-mcp[all]"
        ) from exc

    resolved = settings or StandaloneSettings.from_environment()

    # Parse every credential and policy value before allocating the lazy engine.
    # This keeps synchronous construction safe even when an embedding host calls
    # it from inside an already-running event loop.
    provider = LocalKeyringDekProvider(
        resolved.keyring,
        active_key_id=resolved.active_key_id,
    )
    store_config = PostgresStoreConfig(
        idempotency_index_key=resolved.idempotency_index_key,
        service_namespace=resolved.service_namespace,
    )
    verifier = AsymmetricCapabilityVerifier(
        {
            resolved.capability_key_id: _read_public_key(
                resolved.capability_public_key_file
            )
        },
        issuer=resolved.capability_issuer,
        audience=resolved.capability_audience,
    )
    resolver = StaticCapabilityResolver(
        verifier,
        token=resolved.capability_token,
        principal_id=resolved.principal_id,
    )
    postgres = PostgresRuntime.from_url(
        resolved.database_url,
        schema=resolved.schema,
    )
    service = MemoryService(
        PostgresMemoryStore(
            postgres,
            EnvelopeEncryptor(provider),
            config=store_config,
        )
    )
    server = create_mcp_server(
        service,
        resolver,
        include_app=resolved.include_app,
    )
    return StandaloneRuntime(server=server, postgres=postgres)


async def _serve(runtime: StandaloneRuntime) -> None:
    try:
        await runtime.server.run_async(transport="stdio", show_banner=False)
    finally:
        await runtime.close()


def main() -> None:
    """Run a configured stdio MCP until its client disconnects."""
    try:
        runtime = create_standalone_runtime()
        asyncio.run(_serve(runtime))
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None


__all__ = [
    "StandaloneRuntime",
    "StandaloneSettings",
    "StaticCapabilityResolver",
    "create_standalone_runtime",
    "main",
]
