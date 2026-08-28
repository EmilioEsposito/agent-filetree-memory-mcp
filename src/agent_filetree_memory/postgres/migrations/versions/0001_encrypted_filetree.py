"""Create encrypted file-tree persistence tables.

Revision ID: afm_0001
Revises:
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from agent_filetree_memory.postgres.migrations import schema_from_config

revision = "afm_0001"
down_revision = None
branch_labels = ("agent_filetree_memory",)
depends_on = None

_SCOPE = (
    "workspace_id",
    "agent_profile_id",
)


def _scope_columns(*, primary_key: bool = False) -> list[sa.Column]:
    return [
        sa.Column(name, sa.String(255), primary_key=primary_key, nullable=False)
        for name in _SCOPE
    ]


def _object_foreign_key(schema: str) -> sa.ForeignKeyConstraint:
    local = [*_SCOPE, "object_id"]
    return sa.ForeignKeyConstraint(
        local,
        [f"{schema}.memory_objects.{name}" for name in local],
        ondelete="CASCADE",
    )


def upgrade() -> None:
    """Create package tables in the host-selected existing schema."""

    schema = schema_from_config(op.get_context().config)
    op.create_table(
        "memory_objects",
        *_scope_columns(primary_key=True),
        sa.Column("object_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("object_kind", sa.String(16), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column(
            "lifecycle",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "logical_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "object_kind IN ('root', 'directory', 'document')",
            name="object_kind",
        ),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'deleted')", name="lifecycle"
        ),
        sa.CheckConstraint("current_version >= 1", name="current_version"),
        sa.CheckConstraint("logical_bytes >= 0", name="logical_bytes"),
        schema=schema,
    )
    op.create_index(
        "uq_memory_objects_active_root",
        "memory_objects",
        list(_SCOPE),
        unique=True,
        schema=schema,
        postgresql_where=sa.text(
            "object_kind = 'root' AND lifecycle = 'active'"
        ),
    )
    op.create_index(
        "ix_memory_objects_purge_due",
        "memory_objects",
        ["purge_after"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("lifecycle = 'deleted'"),
    )

    op.create_table(
        "memory_versions",
        *_scope_columns(primary_key=True),
        sa.Column("object_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("version", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("key_id", sa.String(255), nullable=False),
        sa.Column(
            "format_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        _object_foreign_key(schema),
        sa.CheckConstraint("version >= 1", name="version"),
        sa.CheckConstraint("format_version >= 1", name="format_version"),
        schema=schema,
    )
    op.create_index(
        "ix_memory_versions_purge_due",
        "memory_versions",
        ["purge_after"],
        unique=False,
        schema=schema,
        postgresql_where=sa.text("purge_after IS NOT NULL"),
    )

    op.create_table(
        "memory_idempotency",
        *_scope_columns(primary_key=True),
        sa.Column("record_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("lookup_digest", sa.String(64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("key_id", sa.String(255), nullable=False),
        sa.Column(
            "format_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("format_version >= 1", name="format_version"),
        sa.UniqueConstraint(*_SCOPE, "lookup_digest"),
        schema=schema,
    )
    op.create_index(
        "ix_memory_idempotency_expiry",
        "memory_idempotency",
        ["expires_at"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        "memory_audit_events",
        *_scope_columns(primary_key=True),
        sa.Column("event_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("principal_id", sa.String(255)),
        sa.Column("invocation_id", sa.String(255)),
        sa.Column("object_id", sa.String(36)),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(32)),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'denied', 'conflict', 'failed')",
            name="outcome",
        ),
        schema=schema,
    )
    op.create_index(
        "ix_memory_audit_events_scope_time",
        "memory_audit_events",
        [*_SCOPE, "occurred_at"],
        unique=False,
        schema=schema,
    )
    op.create_index(
        "ix_memory_audit_events_expiry",
        "memory_audit_events",
        ["expires_at"],
        unique=False,
        schema=schema,
    )

    op.create_table(
        "memory_quotas",
        *_scope_columns(primary_key=True),
        sa.Column(
            "logical_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "document_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "physical_object_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("logical_bytes >= 0", name="logical_bytes"),
        sa.CheckConstraint("document_count >= 0", name="document_count"),
        sa.CheckConstraint(
            "physical_object_count >= 0", name="physical_object_count"
        ),
        schema=schema,
    )

    op.create_table(
        "memory_rate_buckets",
        *_scope_columns(primary_key=True),
        sa.Column(
            "bucket_started_at",
            sa.DateTime(timezone=True),
            primary_key=True,
        ),
        sa.Column("operation_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("operation_count >= 1", name="operation_count"),
        schema=schema,
    )
    op.create_index(
        "ix_memory_rate_buckets_expiry",
        "memory_rate_buckets",
        ["expires_at"],
        unique=False,
        schema=schema,
    )


def downgrade() -> None:
    """Drop only package-owned tables from the selected schema."""

    schema = schema_from_config(op.get_context().config)
    for table_name in (
        "memory_rate_buckets",
        "memory_quotas",
        "memory_audit_events",
        "memory_idempotency",
        "memory_versions",
        "memory_objects",
    ):
        op.drop_table(table_name, schema=schema)
