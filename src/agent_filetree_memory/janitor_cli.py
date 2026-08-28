"""One-shot console entry point for host-operated retention cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from typing import TYPE_CHECKING

from .domain.errors import ConfigurationError

if TYPE_CHECKING:
    from .postgres import JanitorReport


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise ConfigurationError(f"required janitor setting is missing: {name}")
    return value


def _positive_integer(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    raw = environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw, 10)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be a positive integer") from None
    if value <= 0 or str(value) != raw:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class JanitorSettings:
    """Minimal maintenance configuration; no memory keys are accepted."""

    database_url: str = field(repr=False)
    schema: str = "agent_filetree_memory"
    batch_limit: int = 100
    audit_retention_days: int = 90

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "JanitorSettings":
        values = os.environ if environ is None else environ
        return cls(
            database_url=_required(values, "DATABASE_URL"),
            schema=values.get(
                "AGENT_FILETREE_MEMORY_DATABASE_SCHEMA",
                "agent_filetree_memory",
            ),
            batch_limit=_positive_integer(
                values,
                "AGENT_FILETREE_MEMORY_JANITOR_BATCH_LIMIT",
                default=100,
            ),
            audit_retention_days=_positive_integer(
                values,
                "AGENT_FILETREE_MEMORY_JANITOR_AUDIT_RETENTION_DAYS",
                default=90,
            ),
        )


async def run_janitor_once(
    settings: JanitorSettings | None = None,
    *,
    now: datetime | None = None,
) -> JanitorReport:
    """Run one bounded maintenance transaction and close the owned engine."""
    resolved = settings or JanitorSettings.from_environment()
    try:
        from .postgres import PostgresJanitor, PostgresRuntime

        runtime = PostgresRuntime.from_url(
            resolved.database_url,
            schema=resolved.schema,
        )
    except ImportError:
        raise ConfigurationError(
            "janitor launcher requires agent-filetree-memory-mcp[postgres]"
        ) from None
    try:
        return await PostgresJanitor(
            runtime,
            audit_retention_window=timedelta(
                days=resolved.audit_retention_days
            ),
        ).purge_due(
            now=now or datetime.now(timezone.utc),
            limit=resolved.batch_limit,
        )
    finally:
        await runtime.close()


def main() -> None:
    """Run one batch and write a content-free JSON operator summary."""
    try:
        report = asyncio.run(run_janitor_once())
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    print(json.dumps(report.as_dict(), sort_keys=True))


__all__ = ["JanitorSettings", "main", "run_janitor_once"]
