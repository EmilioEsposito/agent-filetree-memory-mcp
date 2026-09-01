from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastmcp import Client

from agent_filetree_memory.cli import (
    StandaloneRuntime,
    StandaloneSettings,
    StaticCapabilityResolver,
    _serve,
    create_standalone_runtime,
)
from agent_filetree_memory.domain.errors import ConfigurationError
from agent_filetree_memory.domain.models import MemoryAction
from agent_filetree_memory.janitor_cli import JanitorSettings


@pytest.fixture
def standalone_environment(tmp_path) -> dict[str, str]:
    public_key_file = tmp_path / "capability-public.pem"
    public_key_file.write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    encryption_key = base64.b64encode(b"k" * 32).decode("ascii")
    return {
        "DATABASE_URL": (
            "postgresql+asyncpg://memory-user:database-secret@localhost/memory"
        ),
        "AGENT_FILETREE_MEMORY_DATABASE_SCHEMA": "agent_filetree_memory_test",
        "AGENT_FILETREE_MEMORY_KEYRING_JSON": json.dumps(
            {"current-key": encryption_key}
        ),
        "AGENT_FILETREE_MEMORY_ACTIVE_KEY_ID": "current-key",
        "AGENT_FILETREE_MEMORY_IDEMPOTENCY_INDEX_KEY": base64.b64encode(
            b"i" * 32
        ).decode("ascii"),
        "AGENT_FILETREE_MEMORY_CAPABILITY_TOKEN": "pre-issued-secret-token",
        "AGENT_FILETREE_MEMORY_CAPABILITY_PUBLIC_KEY_FILE": str(public_key_file),
        "AGENT_FILETREE_MEMORY_CAPABILITY_KEY_ID": "capability-key",
        "AGENT_FILETREE_MEMORY_CAPABILITY_ISSUER": "standalone-test-issuer",
        "AGENT_FILETREE_MEMORY_CAPABILITY_AUDIENCE": "standalone-test-audience",
        "AGENT_FILETREE_MEMORY_PRINCIPAL_ID": "standalone-principal",
    }


def test_standalone_settings_are_explicit_and_secret_repr_is_redacted(
    standalone_environment,
):
    settings = StandaloneSettings.from_environment(standalone_environment)

    rendered = repr(settings)
    assert settings.include_app is False
    assert settings.schema == "agent_filetree_memory_test"
    assert "database-secret" not in rendered
    assert "pre-issued-secret-token" not in rendered
    assert standalone_environment["AGENT_FILETREE_MEMORY_IDEMPOTENCY_INDEX_KEY"] not in rendered
    assert standalone_environment["AGENT_FILETREE_MEMORY_KEYRING_JSON"] not in rendered
    assert standalone_environment[
        "AGENT_FILETREE_MEMORY_CAPABILITY_PUBLIC_KEY_FILE"
    ] not in rendered


def test_janitor_settings_require_only_database_access_and_hide_credentials():
    environment = {
        "DATABASE_URL": (
            "postgresql+asyncpg://memory-user:janitor-secret@localhost/memory"
        ),
        "AGENT_FILETREE_MEMORY_DATABASE_SCHEMA": "memory_retention",
        "AGENT_FILETREE_MEMORY_JANITOR_BATCH_LIMIT": "25",
        "AGENT_FILETREE_MEMORY_JANITOR_AUDIT_RETENTION_DAYS": "45",
    }

    settings = JanitorSettings.from_environment(environment)

    assert settings.schema == "memory_retention"
    assert settings.batch_limit == 25
    assert settings.audit_retention_days == 45
    assert "janitor-secret" not in repr(settings)
    assert all("KEY" not in name and "TOKEN" not in name for name in environment)


@pytest.mark.parametrize(
    "name",
    [
        "AGENT_FILETREE_MEMORY_JANITOR_BATCH_LIMIT",
        "AGENT_FILETREE_MEMORY_JANITOR_AUDIT_RETENTION_DAYS",
    ],
)
def test_janitor_settings_reject_unbounded_or_ambiguous_limits(name):
    environment = {"DATABASE_URL": "postgresql://localhost/memory", name: "0"}
    with pytest.raises(ConfigurationError, match="positive integer"):
        JanitorSettings.from_environment(environment)


def test_janitor_module_imports_without_postgres_extra_and_explains_it():
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = """
import os
from agent_filetree_memory.janitor_cli import JanitorSettings, main

assert JanitorSettings.from_environment({"DATABASE_URL": "postgresql://localhost/memory"})
os.environ["DATABASE_URL"] = "postgresql://localhost/memory"
try:
    main()
except SystemExit as exc:
    assert "agent-filetree-memory-mcp[postgres]" in str(exc)
else:
    raise AssertionError("missing PostgreSQL extra must fail clearly")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-S", "-c", script],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_standalone_module_imports_without_all_extra_and_explains_it():
    source_root = Path(__file__).resolve().parents[1] / "src"
    script = """
from agent_filetree_memory.cli import main

try:
    main()
except SystemExit as exc:
    assert "agent-filetree-memory-mcp[all]" in str(exc)
else:
    raise AssertionError("missing standalone extras must fail clearly")
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-S", "-c", script],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", ""),
        ("AGENT_FILETREE_MEMORY_KEYRING_JSON", "[]"),
        ("AGENT_FILETREE_MEMORY_IDEMPOTENCY_INDEX_KEY", "not-base64"),
        ("AGENT_FILETREE_MEMORY_ENABLE_APP", "sometimes"),
    ],
)
def test_standalone_settings_fail_closed(
    standalone_environment, name, value
):
    standalone_environment[name] = value

    with pytest.raises(ConfigurationError):
        StandaloneSettings.from_environment(standalone_environment)


async def test_static_resolver_forwards_action_and_principal():
    sentinel = object()

    class Verifier:
        calls: list[tuple[str, MemoryAction, str]] = []

        def verify(
            self,
            token,
            *,
            required_action,
            expected_principal_id,
        ):
            self.calls.append((token, required_action, expected_principal_id))
            return sentinel

    verifier = Verifier()
    resolver = StaticCapabilityResolver(
        verifier,
        token="signed-token",
        principal_id="verified-principal",
    )

    result = await resolver(object(), MemoryAction.READ)

    assert result is sentinel
    assert verifier.calls == [
        ("signed-token", MemoryAction.READ, "verified-principal")
    ]


async def test_runtime_composes_headless_server_without_database_connection(
    standalone_environment,
):
    settings = StandaloneSettings.from_environment(standalone_environment)
    runtime = create_standalone_runtime(settings)

    try:
        async with Client(runtime.server) as client:
            tool_names = {tool.name for tool in await client.list_tools()}
        assert tool_names == {
            "memory_list",
            "memory_read",
            "memory_history_list",
            "memory_history_read",
            "memory_write",
            "memory_append",
            "memory_delete",
        }
        assert runtime.postgres.owns_engine is True
        assert runtime.postgres.schema == "agent_filetree_memory_test"
    finally:
        await runtime.close()


async def test_invalid_configuration_is_safe_inside_running_event_loop(
    standalone_environment,
):
    settings = StandaloneSettings.from_environment(standalone_environment)
    invalid = StandaloneSettings(
        **{
            field: getattr(settings, field)
            for field in settings.__dataclass_fields__
            if field != "active_key_id"
        },
        active_key_id="missing-key",
    )

    with pytest.raises(ConfigurationError):
        create_standalone_runtime(invalid)


async def test_serve_always_closes_runtime():
    class Server:
        async def run_async(self, **_kwargs):
            raise RuntimeError("transport stopped")

    class Postgres:
        closed = False

        async def close(self):
            self.closed = True

    postgres = Postgres()
    runtime = StandaloneRuntime(server=Server(), postgres=postgres)

    with pytest.raises(RuntimeError, match="transport stopped"):
        await _serve(runtime)

    assert postgres.closed is True
