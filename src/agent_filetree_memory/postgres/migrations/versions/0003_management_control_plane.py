"""Add the generic workspace and agent management control plane.

Revision ID: afm_0003
Revises: afm_0002
Create Date: 2026-08-31
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, inspect, text

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    UniqueConstraint,
    func,
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


def _tables(schema: str, *, constraint_namespace: str) -> MetaData:
    """Frozen revision DDL. Never replace with current application metadata."""

    metadata = MetaData(schema=schema)

    Table(
        "workspaces",
        metadata,
        Column("workspace_id", String(32), primary_key=True),
        Column("slug", String(63), nullable=False),
        Column("created_by_principal_id", String(255), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        UniqueConstraint(
            "slug",
            name="uq_afm_workspaces_slug",
        ),
        CheckConstraint(
            "slug = lower(slug)",
            name="ck_afm_workspaces_slug_lowercase",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_workspaces_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_workspaces_integrity_tag_length",
        ),
        schema=schema,
    )

    Table(
        "workspace_members",
        metadata,
        Column(
            "workspace_id",
            String(32),
            ForeignKey(
                f"{schema}.workspaces.workspace_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        Column("principal_id", String(255), nullable=False),
        Column("role", String(16), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        PrimaryKeyConstraint(
            "workspace_id",
            "principal_id",
            name="pk_afm_workspace_members",
        ),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member')",
            name="ck_afm_workspace_members_role",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_workspace_members_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_workspace_members_integrity_tag_length",
        ),
        schema=schema,
    )

    Table(
        "agent_profiles",
        metadata,
        Column("agent_profile_id", String(32), primary_key=True),
        Column(
            "workspace_id",
            String(32),
            ForeignKey(
                f"{schema}.workspaces.workspace_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        Column("slug", String(63), nullable=False),
        Column("display_alias", String(128), nullable=False),
        Column("created_by_principal_id", String(255), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        UniqueConstraint(
            "workspace_id",
            "slug",
            name="uq_afm_agent_profiles_workspace_slug",
        ),
        UniqueConstraint(
            "workspace_id",
            "agent_profile_id",
            name="uq_afm_agent_profiles_workspace_id",
        ),
        CheckConstraint(
            "slug = lower(slug)",
            name="ck_afm_agent_profiles_slug_lowercase",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_agent_profiles_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_agent_profiles_integrity_tag_length",
        ),
        schema=schema,
    )

    Table(
        "agent_grants",
        metadata,
        Column("workspace_id", String(32), nullable=False),
        Column("agent_profile_id", String(32), nullable=False),
        Column("principal_id", String(255), nullable=False),
        Column("role", String(16), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        PrimaryKeyConstraint(
            "agent_profile_id",
            "principal_id",
            name="pk_afm_agent_grants",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_profile_id"],
            [
                f"{schema}.agent_profiles.workspace_id",
                f"{schema}.agent_profiles.agent_profile_id",
            ],
            ondelete="CASCADE",
            name="fk_afm_agent_grants_profile",
        ),
        CheckConstraint(
            "role IN ('reader', 'editor', 'admin')",
            name="ck_afm_agent_grants_role",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_agent_grants_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_agent_grants_integrity_tag_length",
        ),
        schema=schema,
    )

    Table(
        "principal_profiles",
        metadata,
        Column("principal_id", String(255), primary_key=True),
        Column("email", String(254), nullable=False),
        Column("display_name", String(128), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
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
        UniqueConstraint(
            "email",
            name="uq_afm_principal_profiles_email",
        ),
        CheckConstraint(
            "email = lower(email)",
            name="ck_afm_principal_profiles_email_lowercase",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_principal_profiles_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_principal_profiles_integrity_tag_length",
        ),
        schema=schema,
    )

    Table(
        "workspace_invitations",
        metadata,
        Column("invitation_id", String(32), primary_key=True),
        Column(
            "workspace_id",
            String(32),
            ForeignKey(
                f"{schema}.workspaces.workspace_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        Column("email", String(254), nullable=False),
        Column("role", String(16), nullable=False),
        Column("invited_by_principal_id", String(255), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        UniqueConstraint(
            "workspace_id",
            "email",
            name="uq_afm_workspace_invitations_email",
        ),
        CheckConstraint(
            "email = lower(email)",
            name="ck_afm_workspace_invitations_email_lowercase",
        ),
        CheckConstraint(
            "role IN ('admin', 'member')",
            name="ck_afm_workspace_invitations_role",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_workspace_invitations_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_workspace_invitations_integrity_tag_length",
        ),
        schema=schema,
    )

    Table(
        "agent_managers",
        metadata,
        Column("workspace_id", String(32), nullable=False),
        Column("agent_profile_id", String(32), nullable=False),
        Column("principal_id", String(255), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        PrimaryKeyConstraint(
            "agent_profile_id",
            "principal_id",
            name="pk_afm_agent_managers",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_profile_id"],
            [
                f"{schema}.agent_profiles.workspace_id",
                f"{schema}.agent_profiles.agent_profile_id",
            ],
            ondelete="CASCADE",
            name="fk_afm_agent_managers_profile",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_agent_managers_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_agent_managers_integrity_tag_length",
        ),
        schema=schema,
    )

    management_audit_events = Table(
        "management_audit_events",
        metadata,
        Column("event_id", String(32), primary_key=True),
        Column(
            "workspace_id",
            String(32),
            ForeignKey(
                f"{schema}.workspaces.workspace_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        Column("actor_principal_id", String(255), nullable=False),
        Column("action", String(64), nullable=False),
        Column("target_kind", String(32), nullable=False),
        Column("target_id", String(255), nullable=False),
        Column("integrity_version", SmallInteger, nullable=False),
        Column("integrity_tag", LargeBinary(32), nullable=False),
        Column(
            "occurred_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=func.now(),
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_management_audit_events_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name=("ck_afm_management_audit_events_integrity_tag_length"),
        ),
        schema=schema,
    )

    Index(
        "ix_afm_management_audit_workspace_time",
        management_audit_events.c.workspace_id,
        management_audit_events.c.occurred_at,
    )

    for table in metadata.tables.values():
        for item in (*table.constraints, *table.indexes):
            if item.name:
                item.name = item.name.replace("_afm_", f"_{constraint_namespace}_")
    return metadata


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
        column.name: _column_signature(column) for column in expected_table.columns
    }
    if reflected != expected:
        raise RuntimeError(
            f"existing control-plane table {expected_table.name!r} has "
            "incompatible columns"
        )


def _validate_primary_key(inspector, schema: str, expected_table) -> None:
    reflected = tuple(
        inspector.get_pk_constraint(expected_table.name, schema=schema).get(
            "constrained_columns"
        )
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
        for item in inspector.get_unique_constraints(expected_table.name, schema=schema)
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
        for item in inspector.get_foreign_keys(expected_table.name, schema=schema)
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
        for item in inspector.get_check_constraints(table_name, schema=schema)
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
        for item in inspector.get_indexes("management_audit_events", schema=schema)
    }
    if ("workspace_id", "occurred_at") not in indexes:
        raise RuntimeError(
            "existing management_audit_events table is missing its timeline index"
        )


def _validate_existing(schema: str) -> None:
    inspector = inspect(op.get_bind())
    tables = _tables(
        schema,
        constraint_namespace=constraint_namespace_from_config(op.get_context().config),
    )
    for table_name in _TABLE_NAMES:
        expected_table = tables.tables[f"{schema}.{table_name}"]
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
        tables = _tables(
            schema,
            constraint_namespace=constraint_namespace_from_config(
                op.get_context().config
            ),
        )
        for table_name in _TABLE_NAMES:
            tables.tables[f"{schema}.{table_name}"].create(
                bind=op.get_bind(),
                checkfirst=False,
            )
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
    ownership = (
        op.get_bind()
        .execute(
            text(
                f'SELECT ownership FROM "{schema}"."{_INSTALLATION_TABLE}" '
                "WHERE revision = :revision"
            ),
            {"revision": revision},
        )
        .scalar_one_or_none()
    )
    if ownership not in {"created", "adopted"}:
        raise RuntimeError("control-plane installation marker is invalid")
    if ownership == "created":
        for table_name in reversed(_TABLE_NAMES):
            op.drop_table(table_name, schema=schema)
    op.drop_table(_INSTALLATION_TABLE, schema=schema)
