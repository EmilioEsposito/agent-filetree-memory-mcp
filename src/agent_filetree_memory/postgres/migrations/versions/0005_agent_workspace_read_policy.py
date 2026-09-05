"""Add explicit per-agent workspace read policies.

Revision ID: afm_0005
Revises: afm_0004
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, inspect, text

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    func,
)
from agent_filetree_memory.postgres.migrations import (
    constraint_namespace_from_config,
    schema_from_config,
)

revision = "afm_0005"
down_revision = "afm_0004"
branch_labels = None
depends_on = None

_INSTALLATION_TABLE = "_afm_control_plane_installation"
_POLICY_TABLE = "agent_access_policies"


def _tables(schema: str, *, constraint_namespace: str) -> MetaData:
    """Frozen revision DDL. Never replace with current application metadata."""

    metadata = MetaData(schema=schema)

    Table(
        "agent_profiles",
        metadata,
        Column("workspace_id", String(32)),
        Column("agent_profile_id", String(32)),
    )

    Table(
        "agent_access_policies",
        metadata,
        Column("workspace_id", String(32), nullable=False),
        Column("agent_profile_id", String(32), nullable=False),
        Column("access_policy", String(32), nullable=False),
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
        PrimaryKeyConstraint(
            "agent_profile_id",
            name="pk_afm_agent_access_policies",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_profile_id"],
            [
                f"{schema}.agent_profiles.workspace_id",
                f"{schema}.agent_profiles.agent_profile_id",
            ],
            ondelete="CASCADE",
            name="fk_afm_agent_access_policies_profile",
        ),
        CheckConstraint(
            "access_policy IN ('private', 'workspace_read')",
            name="ck_afm_agent_access_policies_access_policy",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_agent_access_policies_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_agent_access_policies_integrity_tag_length",
        ),
        schema=schema,
    )

    for table in metadata.tables.values():
        for item in (*table.constraints, *table.indexes):
            if item.name:
                item.name = item.name.replace("_afm_", f"_{constraint_namespace}_")
    return metadata


def _column_signature(column) -> tuple[type, int | None, bool]:
    affinity = column.type._type_affinity
    length = getattr(column.type, "length", None) if affinity is sa.String else None
    return affinity, length, column.nullable


def _validate_existing(schema: str, expected_table) -> None:
    inspector = inspect(op.get_bind())
    reflected_columns = {
        column["name"]: (
            column["type"]._type_affinity,
            (
                getattr(column["type"], "length", None)
                if column["type"]._type_affinity is sa.String
                else None
            ),
            bool(column["nullable"]),
        )
        for column in inspector.get_columns(_POLICY_TABLE, schema=schema)
    }
    expected_columns = {
        column.name: _column_signature(column) for column in expected_table.columns
    }
    if reflected_columns != expected_columns:
        raise RuntimeError(
            "existing agent_access_policies table has incompatible columns"
        )

    primary_key = tuple(
        inspector.get_pk_constraint(_POLICY_TABLE, schema=schema).get(
            "constrained_columns"
        )
        or ()
    )
    if primary_key != ("agent_profile_id",):
        raise RuntimeError(
            "existing agent_access_policies table has an incompatible primary key"
        )

    foreign_keys = {
        (
            tuple(item.get("constrained_columns") or ()),
            item.get("referred_table"),
            tuple(item.get("referred_columns") or ()),
            str((item.get("options") or {}).get("ondelete", "")).upper(),
        )
        for item in inspector.get_foreign_keys(_POLICY_TABLE, schema=schema)
    }
    if (
        ("workspace_id", "agent_profile_id"),
        "agent_profiles",
        ("workspace_id", "agent_profile_id"),
        "CASCADE",
    ) not in foreign_keys:
        raise RuntimeError(
            "existing agent_access_policies table is missing its agent foreign key"
        )

    reflected_checks = " ".join(
        str(item.get("sqltext") or "").lower()
        for item in inspector.get_check_constraints(_POLICY_TABLE, schema=schema)
    )
    expected_tokens: set[str] = set()
    for constraint in expected_table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        expression = str(constraint.sqltext).lower()
        if "access_policy" in expression:
            expected_tokens.update({"access_policy", "private", "workspace_read"})
        elif "integrity_version" in expression:
            expected_tokens.update({"integrity_version", "1"})
        elif "integrity_tag" in expression:
            expected_tokens.update({"integrity_tag", "octet_length", "32"})
        else:  # pragma: no cover - forces deliberate migration updates
            raise RuntimeError("unrecognized agent access policy check constraint")
    if any(token not in reflected_checks for token in expected_tokens):
        raise RuntimeError(
            "existing agent_access_policies table is missing required checks"
        )


def upgrade() -> None:
    """Create or adopt the policy table without backfilling access."""

    schema = schema_from_config(op.get_context().config)
    tables = _tables(
        schema,
        constraint_namespace=constraint_namespace_from_config(op.get_context().config),
    )
    policy_table = tables.tables[f"{schema}.agent_access_policies"]
    existing = set(inspect(op.get_bind()).get_table_names(schema=schema))
    if _POLICY_TABLE in existing:
        _validate_existing(schema, policy_table)
        ownership = "adopted"
    else:
        policy_table.create(bind=op.get_bind(), checkfirst=False)
        ownership = "created"

    op.execute(
        sa.text(
            f'INSERT INTO "{schema}"."{_INSTALLATION_TABLE}" '
            "(revision, ownership) VALUES (:revision, :ownership)"
        ).bindparams(revision=revision, ownership=ownership)
    )


def downgrade() -> None:
    """Preserve an adopted host table and remove a package-created table."""

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
        raise RuntimeError("agent access policy installation marker is invalid")
    if ownership == "created":
        op.drop_table(_POLICY_TABLE, schema=schema)
    op.get_bind().execute(
        text(
            f'DELETE FROM "{schema}"."{_INSTALLATION_TABLE}" WHERE revision = :revision'
        ),
        {"revision": revision},
    )
