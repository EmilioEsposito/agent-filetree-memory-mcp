"""Bounded, host-operated PostgreSQL retention cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import uuid4

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.errors import IntegrityFailure
from .runtime import PostgresRuntime


@dataclass(frozen=True, slots=True)
class JanitorReport:
    """Exact direct-delete counts for one bounded janitor transaction."""

    cutoff: datetime
    batch_limit: int
    deleted_objects: int
    deleted_versions: int
    deleted_idempotency_records: int
    deleted_rate_buckets: int
    deleted_audit_events: int

    @property
    def total_deleted(self) -> int:
        return (
            self.deleted_objects
            + self.deleted_versions
            + self.deleted_idempotency_records
            + self.deleted_rate_buckets
            + self.deleted_audit_events
        )

    def as_dict(self) -> dict[str, int | str]:
        """Return a stable, JSON-serializable operator summary."""
        return {
            "cutoff": self.cutoff.isoformat(),
            "batch_limit": self.batch_limit,
            "deleted_objects": self.deleted_objects,
            "deleted_versions": self.deleted_versions,
            "deleted_idempotency_records": self.deleted_idempotency_records,
            "deleted_rate_buckets": self.deleted_rate_buckets,
            "deleted_audit_events": self.deleted_audit_events,
            "total_deleted": self.total_deleted,
        }


class PostgresJanitor:
    """Delete due lifecycle rows without requiring memory decryption keys.

    A host must invoke this class or the packaged console entry point. Nothing
    in the request-serving runtime starts a scheduler. Every directly targeted
    table is capped independently by ``limit`` and selected with
    ``FOR UPDATE SKIP LOCKED`` so multiple host jobs can cooperate safely.
    """

    def __init__(
        self,
        runtime: PostgresRuntime,
        *,
        audit_retention_window: timedelta = timedelta(days=90),
    ) -> None:
        if not isinstance(runtime, PostgresRuntime):
            raise TypeError("runtime must be a PostgresRuntime")
        if (
            not isinstance(audit_retention_window, timedelta)
            or audit_retention_window <= timedelta(0)
        ):
            raise ValueError("audit_retention_window must be positive")
        self.runtime = runtime
        self.tables = runtime.tables
        self.audit_retention_window = audit_retention_window

    async def purge_due(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> JanitorReport:
        """Process at most ``limit`` selected rows from each lifecycle table."""
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        cutoff = now.astimezone(timezone.utc)

        async with self.runtime.session() as session, session.begin():
            deleted_objects = await self._purge_objects(
                session,
                cutoff=cutoff,
                limit=limit,
            )

            versions = self.tables.versions
            objects = self.tables.objects
            deleted_versions = await self._delete_selected(
                session,
                versions,
                where=(
                    versions.c.purge_after <= cutoff,
                    ~select(1)
                    .where(
                        objects.c.workspace_id == versions.c.workspace_id,
                        objects.c.agent_profile_id
                        == versions.c.agent_profile_id,
                        objects.c.object_id == versions.c.object_id,
                        objects.c.current_version == versions.c.version,
                    )
                    .exists(),
                ),
                order_by=(versions.c.purge_after,),
                limit=limit,
            )
            idempotency = self.tables.idempotency
            deleted_idempotency = await self._delete_selected(
                session,
                idempotency,
                where=(idempotency.c.expires_at <= cutoff,),
                order_by=(idempotency.c.expires_at,),
                limit=limit,
            )
            rate_buckets = self.tables.rate_buckets
            deleted_rate_buckets = await self._delete_selected(
                session,
                rate_buckets,
                where=(rate_buckets.c.expires_at <= cutoff,),
                order_by=(rate_buckets.c.expires_at,),
                limit=limit,
            )
            audit_events = self.tables.audit_events
            deleted_audit_events = await self._delete_selected(
                session,
                audit_events,
                where=(audit_events.c.expires_at <= cutoff,),
                order_by=(audit_events.c.expires_at,),
                limit=limit,
            )

        return JanitorReport(
            cutoff=cutoff,
            batch_limit=limit,
            deleted_objects=deleted_objects,
            deleted_versions=deleted_versions,
            deleted_idempotency_records=deleted_idempotency,
            deleted_rate_buckets=deleted_rate_buckets,
            deleted_audit_events=deleted_audit_events,
        )

    async def _purge_objects(
        self,
        session: AsyncSession,
        *,
        cutoff: datetime,
        limit: int,
    ) -> int:
        objects = self.tables.objects
        rows = (
            await session.execute(
                select(objects)
                .where(
                    objects.c.lifecycle == "deleted",
                    objects.c.purge_after <= cutoff,
                )
                .order_by(
                    objects.c.purge_after,
                    objects.c.workspace_id,
                    objects.c.agent_profile_id,
                    objects.c.object_id,
                )
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).mappings().all()
        for row in rows:
            scope = and_(
                objects.c.workspace_id == row["workspace_id"],
                objects.c.agent_profile_id == row["agent_profile_id"],
            )
            removed = await session.execute(
                delete(objects).where(
                    scope,
                    objects.c.object_id == row["object_id"],
                    objects.c.lifecycle == "deleted",
                    objects.c.purge_after <= cutoff,
                )
            )
            if removed.rowcount != 1:
                raise IntegrityFailure("janitor object batch changed concurrently")

            quotas = self.tables.quotas
            quota_changed = await session.execute(
                update(quotas)
                .where(
                    quotas.c.workspace_id == row["workspace_id"],
                    quotas.c.agent_profile_id == row["agent_profile_id"],
                    quotas.c.physical_object_count > 0,
                )
                .values(
                    physical_object_count=quotas.c.physical_object_count - 1,
                    updated_at=cutoff,
                )
            )
            if quota_changed.rowcount != 1:
                raise IntegrityFailure("janitor found an inconsistent object quota")

            await session.execute(
                insert(self.tables.audit_events).values(
                    workspace_id=row["workspace_id"],
                    agent_profile_id=row["agent_profile_id"],
                    event_id=str(uuid4()),
                    principal_id=None,
                    invocation_id=None,
                    object_id=row["object_id"],
                    action="memory:purge",
                    outcome="succeeded",
                    reason_code=None,
                    occurred_at=cutoff,
                    expires_at=cutoff + self.audit_retention_window,
                )
            )
        return len(rows)

    async def _delete_selected(
        self,
        session: AsyncSession,
        table: Any,
        *,
        where: Sequence[Any],
        order_by: Sequence[Any],
        limit: int,
    ) -> int:
        primary_key = tuple(table.primary_key.columns)
        selected = (
            await session.execute(
                select(*primary_key)
                .where(*where)
                .order_by(*order_by, *primary_key)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).mappings().all()
        if not selected:
            return 0
        exact_rows = or_(
            *(
                and_(
                    *(column == row[column.name] for column in primary_key)
                )
                for row in selected
            )
        )
        removed = await session.execute(delete(table).where(exact_rows))
        if removed.rowcount != len(selected):
            raise IntegrityFailure("janitor batch changed concurrently")
        return len(selected)


__all__ = ["JanitorReport", "PostgresJanitor"]
