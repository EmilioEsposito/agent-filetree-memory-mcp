"""SQLAlchemy table factories for the PostgreSQL adapter.

The database schema is intentionally configurable so a host can place the
tables alongside its own application tables without giving this package
control of database or schema creation.  Virtual paths and memory content are
never columns: names live only inside encrypted directory manifests.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

from ..domain.errors import ConfigurationError

DEFAULT_SCHEMA = "agent_filetree_memory"
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_RESERVED_SCHEMAS = {"information_schema"}
_SCOPE_COLUMNS = (
    "workspace_id",
    "agent_profile_id",
)


def validate_schema_name(schema: str) -> str:
    """Return a conservative, unquoted PostgreSQL schema identifier.

    Schema names cannot be bound as SQL parameters.  Restricting them to
    lowercase PostgreSQL identifiers makes every later use safe and keeps
    Alembic, SQLAlchemy, and hand-run administrative queries consistent.
    """

    if (
        not isinstance(schema, str)
        or not _SCHEMA_NAME.fullmatch(schema)
        or schema.startswith("pg_")
        or schema in _RESERVED_SCHEMAS
    ):
        raise ConfigurationError(
            "database schema must be a lowercase PostgreSQL identifier"
        )
    return schema


def _scope_columns(*, primary_key: bool = False) -> list[Column]:
    return [
        Column(name, String(255), primary_key=primary_key, nullable=False)
        for name in _SCOPE_COLUMNS
    ]


def _scope_object_foreign_key(
    *, target: str = "memory_objects", ondelete: str = "CASCADE"
) -> ForeignKeyConstraint:
    local = [*_SCOPE_COLUMNS, "object_id"]
    remote = [f"{target}.{name}" for name in local]
    return ForeignKeyConstraint(local, remote, ondelete=ondelete)


@dataclass(frozen=True, slots=True)
class PostgresTables:
    """All tables owned by one package installation in one schema."""

    metadata: MetaData
    objects: Table
    versions: Table
    idempotency: Table
    audit_events: Table
    quotas: Table
    rate_buckets: Table


@lru_cache(maxsize=32)
def tables_for_schema(schema: str = DEFAULT_SCHEMA) -> PostgresTables:
    """Build (and cache) table definitions for a validated schema."""

    schema = validate_schema_name(schema)
    metadata = MetaData(
        schema=schema,
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )

    objects = Table(
        "memory_objects",
        metadata,
        *_scope_columns(primary_key=True),
        Column("object_id", String(36), primary_key=True, nullable=False),
        Column("object_kind", String(16), nullable=False),
        Column("current_version", Integer, nullable=False),
        Column("lifecycle", String(16), nullable=False, server_default="active"),
        Column("logical_bytes", BigInteger, nullable=False, server_default="0"),
        Column("purge_after", DateTime(timezone=True)),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        CheckConstraint(
            "object_kind IN ('root', 'directory', 'document')",
            name="object_kind",
        ),
        CheckConstraint(
            "lifecycle IN ('active', 'deleted')", name="lifecycle"
        ),
        CheckConstraint("current_version >= 1", name="current_version"),
        CheckConstraint("logical_bytes >= 0", name="logical_bytes"),
    )
    Index(
        "uq_memory_objects_active_root",
        *[objects.c[name] for name in _SCOPE_COLUMNS],
        unique=True,
        postgresql_where=text("object_kind = 'root' AND lifecycle = 'active'"),
    )
    Index(
        "ix_memory_objects_purge_due",
        objects.c.purge_after,
        postgresql_where=text("lifecycle = 'deleted'"),
    )

    versions = Table(
        "memory_versions",
        metadata,
        *_scope_columns(primary_key=True),
        Column("object_id", String(36), primary_key=True, nullable=False),
        Column("version", Integer, primary_key=True, nullable=False),
        Column("ciphertext", LargeBinary, nullable=False),
        Column("wrapped_dek", LargeBinary, nullable=False),
        Column("provider_id", String(128), nullable=False),
        Column("key_id", String(255), nullable=False),
        Column("format_version", Integer, nullable=False, server_default="1"),
        Column("purge_after", DateTime(timezone=True)),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        _scope_object_foreign_key(),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("format_version >= 1", name="format_version"),
    )
    Index(
        "ix_memory_versions_purge_due",
        versions.c.purge_after,
        postgresql_where=text("purge_after IS NOT NULL"),
    )

    idempotency = Table(
        "memory_idempotency",
        metadata,
        *_scope_columns(primary_key=True),
        Column("record_id", String(36), primary_key=True, nullable=False),
        Column("lookup_digest", String(64), nullable=False),
        Column("ciphertext", LargeBinary, nullable=False),
        Column("wrapped_dek", LargeBinary, nullable=False),
        Column("provider_id", String(128), nullable=False),
        Column("key_id", String(255), nullable=False),
        Column("format_version", Integer, nullable=False, server_default="1"),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("format_version >= 1", name="format_version"),
        UniqueConstraint(*_SCOPE_COLUMNS, "lookup_digest"),
    )
    Index("ix_memory_idempotency_expiry", idempotency.c.expires_at)

    audit_events = Table(
        "memory_audit_events",
        metadata,
        *_scope_columns(primary_key=True),
        Column("event_id", String(36), primary_key=True, nullable=False),
        Column("principal_id", String(255)),
        Column("invocation_id", String(255)),
        Column("object_id", String(36)),
        Column("action", String(32), nullable=False),
        Column("outcome", String(16), nullable=False),
        Column("reason_code", String(32)),
        Column(
            "occurred_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        CheckConstraint(
            "outcome IN ('succeeded', 'denied', 'conflict', 'failed')",
            name="outcome",
        ),
    )
    Index(
        "ix_memory_audit_events_scope_time",
        *[audit_events.c[name] for name in _SCOPE_COLUMNS],
        audit_events.c.occurred_at,
    )
    Index("ix_memory_audit_events_expiry", audit_events.c.expires_at)

    quotas = Table(
        "memory_quotas",
        metadata,
        *_scope_columns(primary_key=True),
        Column("logical_bytes", BigInteger, nullable=False, server_default="0"),
        Column("document_count", Integer, nullable=False, server_default="0"),
        Column(
            "physical_object_count", Integer, nullable=False, server_default="0"
        ),
        Column(
            "updated_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        CheckConstraint("logical_bytes >= 0", name="logical_bytes"),
        CheckConstraint("document_count >= 0", name="document_count"),
        CheckConstraint(
            "physical_object_count >= 0", name="physical_object_count"
        ),
    )

    rate_buckets = Table(
        "memory_rate_buckets",
        metadata,
        *_scope_columns(primary_key=True),
        Column("bucket_started_at", DateTime(timezone=True), primary_key=True),
        Column("operation_count", Integer, nullable=False),
        Column("expires_at", DateTime(timezone=True), nullable=False),
        CheckConstraint("operation_count >= 1", name="operation_count"),
    )
    Index("ix_memory_rate_buckets_expiry", rate_buckets.c.expires_at)

    return PostgresTables(
        metadata=metadata,
        objects=objects,
        versions=versions,
        idempotency=idempotency,
        audit_events=audit_events,
        quotas=quotas,
        rate_buckets=rate_buckets,
    )


__all__ = [
    "DEFAULT_SCHEMA",
    "PostgresTables",
    "tables_for_schema",
    "validate_schema_name",
]
