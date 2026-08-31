from __future__ import annotations

from pathlib import Path
import os

from alembic.config import Config
import pytest

from agent_filetree_memory.domain.errors import ConfigurationError
from agent_filetree_memory.postgres import (
    PostgresRuntime,
    PostgresStoreConfig,
    validate_schema_name,
)
from agent_filetree_memory.postgres.migrations import (
    CONSTRAINT_NAMESPACE_ATTRIBUTE,
    SCHEMA_ATTRIBUTE,
    configure_host_alembic,
    migration_metadata,
    package_version_location,
)


@pytest.mark.parametrize(
    "value",
    ["", "MixedCase", "has-dash", "quoted.name", "pg_catalog", "information_schema"],
)
def test_schema_validation_rejects_unsafe_or_ambiguous_names(value):
    with pytest.raises(ConfigurationError):
        validate_schema_name(value)


def test_schema_validation_and_metadata_are_configurable():
    assert validate_schema_name("memory_2") == "memory_2"
    metadata = migration_metadata("memory_2")
    assert metadata.schema == "memory_2"
    assert {table.name for table in metadata.tables.values()} == {
        "_afm_control_plane_installation",
        "agent_grants",
        "agent_managers",
        "agent_profiles",
        "management_audit_events",
        "memory_audit_events",
        "memory_idempotency",
        "memory_objects",
        "memory_quotas",
        "memory_rate_buckets",
        "memory_versions",
        "principal_profiles",
        "workspace_invitations",
        "workspace_members",
        "workspace_policies",
        "workspaces",
    }
    objects = metadata.tables["memory_2.memory_objects"]
    assert {column.name for column in objects.primary_key.columns} == {
        "workspace_id",
        "agent_profile_id",
        "object_id",
    }
    audit = metadata.tables["memory_2.memory_audit_events"]
    assert "principal_id" in audit.c
    assert not audit.c.principal_id.primary_key

    custom_names = migration_metadata(
        "memory_2",
        constraint_namespace="memory_2",
    )
    workspace_constraints = {
        constraint.name
        for constraint in custom_names.tables[
            "memory_2.workspace_members"
        ].constraints
    }
    assert "ck_memory_2_workspace_members_role" in workspace_constraints


def test_store_config_requires_secret_blind_index_key_and_opaque_namespace():
    with pytest.raises(ValueError, match="at least 32 bytes"):
        PostgresStoreConfig(idempotency_index_key=b"short")
    with pytest.raises(ValueError, match="service_namespace"):
        PostgresStoreConfig(
            idempotency_index_key=b"x" * 32,
            service_namespace="not opaque whitespace",
        )
    with pytest.raises(ValueError, match="cannot exceed 64"):
        PostgresStoreConfig(
            idempotency_index_key=b"x" * 32,
            max_path_depth=65,
        )
    with pytest.raises(ValueError, match="max_versions_per_object"):
        PostgresStoreConfig(
            idempotency_index_key=b"x" * 32,
            max_versions_per_object=0,
        )
    marker = b"0123456789abcdef" * 2
    config = PostgresStoreConfig(idempotency_index_key=marker)
    assert marker.decode() not in repr(config)


def test_packaged_revision_and_host_configuration():
    location = package_version_location()
    assert isinstance(location, Path)
    assert (location / "0001_encrypted_filetree.py").is_file()
    assert (location / "0002_normalize_constraint_names.py").is_file()
    assert (location / "0003_management_control_plane.py").is_file()
    assert (location / "0004_workspace_authorization_policies.py").is_file()
    config = Config()
    configure_host_alembic(
        config,
        schema="custom_memory",
        constraint_namespace="custom_memory",
    )
    assert config.attributes[SCHEMA_ATTRIBUTE] == "custom_memory"
    assert (
        config.attributes[CONSTRAINT_NAMESPACE_ATTRIBUTE]
        == "custom_memory"
    )
    assert str(location) in config.get_main_option("version_locations")


def test_host_configuration_preserves_implicit_versions_directory(tmp_path):
    host = tmp_path / "host_migrations"
    host_versions = host / "versions"
    host_versions.mkdir(parents=True)
    config = Config()
    config.set_main_option("script_location", str(host))

    configure_host_alembic(config, schema="custom_memory")

    locations = config.get_main_option("version_locations").split(os.pathsep)
    assert str(host_versions) in locations
    assert str(package_version_location()) in locations


def test_initial_revision_is_frozen_from_current_metadata():
    source = (
        package_version_location() / "0001_encrypted_filetree.py"
    ).read_text(encoding="utf-8")
    assert "tables_for_schema" not in source
    assert "postgres.schema" not in source
    assert "op.create_table" in source


async def test_borrowed_runtime_never_owns_or_disposes_host_engine():
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("factory should not be called by close")

    runtime = PostgresRuntime.from_session_factory(factory, schema="borrowed_memory")
    assert not runtime.owns_engine
    assert runtime.engine is None
    await runtime.close()
    assert not called


async def test_url_runtime_owns_its_engine_without_connecting():
    runtime = PostgresRuntime.from_url(
        "postgresql://user:password@localhost/example", schema="owned_memory"
    )
    assert runtime.owns_engine
    assert runtime.engine is not None
    assert runtime.engine.url.drivername == "postgresql+asyncpg"
    await runtime.close()


def test_invalid_url_does_not_retain_parser_exception_or_credentials():
    marker = "sensitive-password-marker"
    with pytest.raises(ConfigurationError) as raised:
        PostgresRuntime.from_url(f"not a url:{marker}", schema="owned_memory")
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__
    assert marker not in str(raised.value)
