"""Add the generic workspace and agent management control plane.

Revision ID: afm_0003
Revises: afm_0002
Create Date: 2026-08-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, inspect, text

from agent_filetree_memory.control_plane.namespace_store import (
    namespace_tables_for_schema,
)
from agent_filetree_memory.postgres.migrations import (
    constraint_namespace_from_config,
    schema_from_config,
)

revision = "afm_0003"
down_revision = "afm_0002"
branch_labels = None
depends_on = None

_INSTALLATION_TABLE = "_afm_control_plane_installation"
_TABLE_NAMES = (
    "workspaces",
    "workspace_members",
    "agent_profiles",
    "agent_grants",
    "principal_profiles",
    "workspace_invitations",
    "agent_managers",
    "management_audit_events",
)


def _column_signature(column) -> tuple[type, int | None, bool]:
    column_type = column.type
    affinity = column_type._type_affinity
    length = getattr(column_type, "length", None) if affinity is sa.String else None
    return affinity, length, column.nullable


def _validate_columns(inspector, schema: str, expected_table) -> None:
    reflected = {
        column["name"]: (
            column["type"]._type_affinity,
            (
                getattr(column["type"], "length", None)
                if column["type"]._type_affinity is sa.String
                else None
            ),
            bool(column["nullable"]),
        )
        for column in inspector.get_columns(expected_table.name, schema=schema)
    }
    expected = {
        column.name: _column_signature(column)
        for column in expected_table.columns
    }
    if reflected != expected:
        raise RuntimeError(
            f"existing control-plane table {expected_table.name!r} has "
            "incompatible columns"
        )


def _validate_primary_key(inspector, schema: str, expected_table) -> None:
    reflected = tuple(
        inspector.get_pk_constraint(
            expected_table.name, schema=schema
        ).get("constrained_columns")
        or ()
    )
    expected = tuple(column.name for column in expected_table.primary_key)
    if reflected != expected:
        raise RuntimeError(
            f"existing control-plane table {expected_table.name!r} has "
            "an incompatible primary key"
        )


def _validate_unique_constraints(inspector, schema: str, expected_table) -> None:
    reflected = {
        tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(
            expected_table.name, schema=schema
        )
    }
    expected = {
        tuple(column.name for column in constraint.columns)
        for constraint in expected_table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    if not expected.issubset(reflected):
        raise RuntimeError(
            f"existing control-plane table {expected_table.name!r} is "
            "missing a required unique constraint"
        )


def _validate_foreign_keys(inspector, schema: str, expected_table) -> None:
    reflected = {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
            str((item.get("options") or {}).get("ondelete", "")).upper(),
        )
        for item in inspector.get_foreign_keys(
            expected_table.name, schema=schema
        )
    }
    expected = {
        (
            tuple(element.parent.name for element in constraint.elements),
            constraint.elements[0].column.table.name,
            tuple(element.column.name for element in constraint.elements),
            str(constraint.ondelete or "").upper(),
        )
        for constraint in expected_table.foreign_key_constraints
    }
    if not expected.issubset(reflected):
        raise RuntimeError(
            f"existing control-plane table {expected_table.name!r} is "
            "missing a required foreign key"
        )


def _normalized_checks(inspector, schema: str, table_name: str) -> str:
    return " ".join(
        str(item.get("sqltext") or "").lower()
        for item in inspector.get_check_constraints(
            table_name, schema=schema
        )
    )


def _validate_checks(inspector, schema: str, expected_table) -> None:
    reflected = _normalized_checks(inspector, schema, expected_table.name)
    for constraint in expected_table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        expression = str(constraint.sqltext).lower()
        if "slug = lower(slug)" in expression:
            required = ("slug", "lower")
        elif "email = lower(email)" in expression:
            required = ("email", "lower")
        elif "integrity_version = 1" in expression:
            required = ("integrity_version", "1")
        elif "octet_length(integrity_tag) = 32" in expression:
            required = ("integrity_tag", "octet_length", "32")
        elif "role in" in expression:
            required = tuple(
                token
                for token in (
                    "role",
                    "owner",
                    "admin",
                    "member",
                    "reader",
                    "editor",
                )
                if token in expression
            )
        else:  # pragma: no cover - forces deliberate migration updates
            raise RuntimeError(
                f"unrecognized control-plane check on {expected_table.name!r}"
            )
        if any(token not in reflected for token in required):
            raise RuntimeError(
                f"existing control-plane table {expected_table.name!r} is "
                "missing a required check constraint"
            )


def _validate_audit_index(inspector, schema: str) -> None:
    indexes = {
        tuple(item.get("column_names") or ())
        for item in inspector.get_indexes(
            "management_audit_events", schema=schema
        )
    }
    if ("workspace_id", "occurred_at") not in indexes:
        raise RuntimeError(
            "existing management_audit_events table is missing its timeline index"
        )


def _validate_existing(schema: str) -> None:
    inspector = inspect(op.get_bind())
    tables = namespace_tables_for_schema(
        schema,
        constraint_namespace=constraint_namespace_from_config(
            op.get_context().config
        ),
    )
    for expected_table in tables.metadata.sorted_tables:
        _validate_columns(inspector, schema, expected_table)
        _validate_primary_key(inspector, schema, expected_table)
        _validate_unique_constraints(inspector, schema, expected_table)
        _validate_foreign_keys(inspector, schema, expected_table)
        _validate_checks(inspector, schema, expected_table)
    _validate_audit_index(inspector, schema)


def upgrade() -> None:
    """Create a fresh control plane or adopt one complete compatible schema."""

    schema = schema_from_config(op.get_context().config)
    existing = set(inspect(op.get_bind()).get_table_names(schema=schema))
    present = set(_TABLE_NAMES) & existing
    if present and present != set(_TABLE_NAMES):
        missing = ", ".join(sorted(set(_TABLE_NAMES) - present))
        raise RuntimeError(
            "partial control-plane schema cannot be adopted; missing: " + missing
        )
    if present:
        _validate_existing(schema)
        ownership = "adopted"
    else:
        tables = namespace_tables_for_schema(
            schema,
            constraint_namespace=constraint_namespace_from_config(
                op.get_context().config
            ),
        )
        for table in tables.metadata.sorted_tables:
            table.create(bind=op.get_bind(), checkfirst=False)
        ownership = "created"

    marker_table = op.create_table(
        _INSTALLATION_TABLE,
        sa.Column("revision", sa.String(32), nullable=False),
        sa.Column("ownership", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("revision"),
        sa.CheckConstraint(
            "ownership IN ('created', 'adopted')",
            name="ck_afm_control_plane_installation_ownership",
        ),
        schema=schema,
    )
    op.bulk_insert(
        marker_table,
        [{"revision": revision, "ownership": ownership}],
    )


def downgrade() -> None:
    """Preserve host-owned tables and remove only package-created installs."""

    schema = schema_from_config(op.get_context().config)
    existing = set(inspect(op.get_bind()).get_table_names(schema=schema))
    if _INSTALLATION_TABLE not in existing:
        return
    ownership = op.get_bind().execute(
        text(
            f'SELECT ownership FROM "{schema}"."{_INSTALLATION_TABLE}" '
            "WHERE revision = :revision"
        ),
        {"revision": revision},
    ).scalar_one_or_none()
    if ownership not in {"created", "adopted"}:
        raise RuntimeError("control-plane installation marker is invalid")
    if ownership == "created":
        for table_name in reversed(_TABLE_NAMES):
            op.drop_table(table_name, schema=schema)
    op.drop_table(_INSTALLATION_TABLE, schema=schema)
