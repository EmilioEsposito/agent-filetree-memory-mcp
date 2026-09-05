"""Add provider-neutral workspace authorization policies.

Revision ID: afm_0004
Revises: afm_0003
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
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Table,
    func,
)
from agent_filetree_memory.postgres.migrations import (
    constraint_namespace_from_config,
    schema_from_config,
)

revision = "afm_0004"
down_revision = "afm_0003"
branch_labels = None
depends_on = None

_INSTALLATION_TABLE = "_afm_control_plane_installation"
_POLICY_TABLE = "workspace_policies"


def _tables(schema: str, *, constraint_namespace: str) -> MetaData:
    """Frozen revision DDL. Never replace with current application metadata."""

    metadata = MetaData(schema=schema)

    Table("workspaces", metadata, Column("workspace_id", String(32), primary_key=True))

    Table(
        "workspace_policies",
        metadata,
        Column(
            "workspace_id",
            String(32),
            ForeignKey(
                f"{schema}.workspaces.workspace_id",
                ondelete="CASCADE",
            ),
            primary_key=True,
        ),
        Column("admission_policy", String(32), nullable=False),
        Column("agent_creation_policy", String(32), nullable=False),
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
        CheckConstraint(
            "admission_policy IN ('invite_only', 'all_authenticated', "
            "'external_entitlement')",
            name="ck_afm_workspace_policies_admission_policy",
        ),
        CheckConstraint(
            "agent_creation_policy IN ('admins_only', 'all_members')",
            name="ck_afm_workspace_policies_agent_creation_policy",
        ),
        CheckConstraint(
            "integrity_version = 1",
            name="ck_afm_workspace_policies_integrity_version",
        ),
        CheckConstraint(
            "octet_length(integrity_tag) = 32",
            name="ck_afm_workspace_policies_integrity_tag_length",
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
        raise RuntimeError("existing workspace_policies table has incompatible columns")

    primary_key = tuple(
        inspector.get_pk_constraint(_POLICY_TABLE, schema=schema).get(
            "constrained_columns"
        )
        or ()
    )
    if primary_key != ("workspace_id",):
        raise RuntimeError(
            "existing workspace_policies table has an incompatible primary key"
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
        ("workspace_id",),
        "workspaces",
        ("workspace_id",),
        "CASCADE",
    ) not in foreign_keys:
        raise RuntimeError(
            "existing workspace_policies table is missing its workspace foreign key"
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
        if "admission_policy" in expression:
            expected_tokens.update(
                {
                    "admission_policy",
                    "invite_only",
                    "all_authenticated",
                    "external_entitlement",
                }
            )
        elif "agent_creation_policy" in expression:
            expected_tokens.update(
                {"agent_creation_policy", "admins_only", "all_members"}
            )
        elif "integrity_version" in expression:
            expected_tokens.update({"integrity_version", "1"})
        elif "integrity_tag" in expression:
            expected_tokens.update({"integrity_tag", "octet_length", "32"})
        else:  # pragma: no cover - forces deliberate migration updates
            raise RuntimeError("unrecognized workspace policy check constraint")
    if any(token not in reflected_checks for token in expected_tokens):
        raise RuntimeError(
            "existing workspace_policies table is missing required checks"
        )


def upgrade() -> None:
    """Create or safely adopt the policy table without backfilling grants."""

    schema = schema_from_config(op.get_context().config)
    tables = _tables(
        schema,
        constraint_namespace=constraint_namespace_from_config(op.get_context().config),
    )
    policy_table = tables.tables[f"{schema}.workspace_policies"]
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
        raise RuntimeError("workspace policy installation marker is invalid")
    if ownership == "created":
        op.drop_table(_POLICY_TABLE, schema=schema)
    op.get_bind().execute(
        text(
            f'DELETE FROM "{schema}"."{_INSTALLATION_TABLE}" WHERE revision = :revision'
        ),
        {"revision": revision},
    )
