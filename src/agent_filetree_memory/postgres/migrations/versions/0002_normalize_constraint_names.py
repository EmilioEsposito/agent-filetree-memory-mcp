"""Normalize package constraint names to the public metadata convention.

Revision ID: afm_0002
Revises: afm_0001
Create Date: 2026-08-28
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

from agent_filetree_memory.postgres.migrations import schema_from_config

revision = "afm_0002"
down_revision = "afm_0001"
branch_labels = None
depends_on = None


_CHECKS = (
    (
        "memory_objects",
        "object_kind",
        "ck_memory_objects_object_kind",
        "object_kind IN ('root', 'directory', 'document')",
    ),
    (
        "memory_objects",
        "lifecycle",
        "ck_memory_objects_lifecycle",
        "lifecycle IN ('active', 'deleted')",
    ),
    (
        "memory_objects",
        "current_version",
        "ck_memory_objects_current_version",
        "current_version >= 1",
    ),
    (
        "memory_objects",
        "logical_bytes",
        "ck_memory_objects_logical_bytes",
        "logical_bytes >= 0",
    ),
    (
        "memory_versions",
        "version",
        "ck_memory_versions_version",
        "version >= 1",
    ),
    (
        "memory_versions",
        "format_version",
        "ck_memory_versions_format_version",
        "format_version >= 1",
    ),
    (
        "memory_idempotency",
        "format_version",
        "ck_memory_idempotency_format_version",
        "format_version >= 1",
    ),
    (
        "memory_audit_events",
        "outcome",
        "ck_memory_audit_events_outcome",
        "outcome IN ('succeeded', 'denied', 'conflict', 'failed')",
    ),
    (
        "memory_quotas",
        "logical_bytes",
        "ck_memory_quotas_logical_bytes",
        "logical_bytes >= 0",
    ),
    (
        "memory_quotas",
        "document_count",
        "ck_memory_quotas_document_count",
        "document_count >= 0",
    ),
    (
        "memory_quotas",
        "physical_object_count",
        "ck_memory_quotas_physical_object_count",
        "physical_object_count >= 0",
    ),
    (
        "memory_rate_buckets",
        "operation_count",
        "ck_memory_rate_buckets_operation_count",
        "operation_count >= 1",
    ),
)

_IDEMPOTENCY_UNIQUE_OLD = (
    "memory_idempotency_workspace_id_agent_profile_id_lookup_dig_key"
)
_IDEMPOTENCY_UNIQUE_NEW = (
    "uq_memory_idempotency_workspace_id_agent_profile_id_lookup_digest"
)
_IDEMPOTENCY_UNIQUE_COLUMNS = (
    "workspace_id",
    "agent_profile_id",
    "lookup_digest",
)


def _idempotency_unique_name(schema: str) -> str:
    """Resolve the initial revision's host-dependent unique-constraint name."""

    constraints = inspect(op.get_bind()).get_unique_constraints(
        "memory_idempotency",
        schema=schema,
    )
    matches = [
        constraint["name"]
        for constraint in constraints
        if tuple(constraint.get("column_names") or ())
        == _IDEMPOTENCY_UNIQUE_COLUMNS
        and constraint.get("name")
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "expected exactly one memory_idempotency unique constraint "
            "covering workspace_id, agent_profile_id, and lookup_digest"
        )
    return str(matches[0])


def upgrade() -> None:
    """Replace legacy names with names emitted by migration metadata."""

    schema = schema_from_config(op.get_context().config)
    for table, old_name, new_name, condition in _CHECKS:
        op.drop_constraint(old_name, table, schema=schema, type_="check")
        op.create_check_constraint(
            op.f(new_name),
            table,
            condition,
            schema=schema,
        )

    op.drop_constraint(
        _idempotency_unique_name(schema),
        "memory_idempotency",
        schema=schema,
        type_="unique",
    )
    op.create_unique_constraint(
        op.f(_IDEMPOTENCY_UNIQUE_NEW),
        "memory_idempotency",
        list(_IDEMPOTENCY_UNIQUE_COLUMNS),
        schema=schema,
    )


def downgrade() -> None:
    """Restore the names created by the initial package revision."""

    schema = schema_from_config(op.get_context().config)
    op.drop_constraint(
        op.f(_IDEMPOTENCY_UNIQUE_NEW),
        "memory_idempotency",
        schema=schema,
        type_="unique",
    )
    op.create_unique_constraint(
        _IDEMPOTENCY_UNIQUE_OLD,
        "memory_idempotency",
        list(_IDEMPOTENCY_UNIQUE_COLUMNS),
        schema=schema,
    )

    for table, old_name, new_name, condition in reversed(_CHECKS):
        op.drop_constraint(
            op.f(new_name),
            table,
            schema=schema,
            type_="check",
        )
        op.create_check_constraint(
            old_name,
            table,
            condition,
            schema=schema,
        )
