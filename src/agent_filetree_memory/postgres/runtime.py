"""Explicitly-owned PostgreSQL runtime resources."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..domain.errors import ConfigurationError
from .schema import DEFAULT_SCHEMA, PostgresTables, tables_for_schema, validate_schema_name

SessionFactory = Callable[[], Any]


def _asyncpg_url(url: str | URL) -> URL:
    try:
        parsed = make_url(url)
    except Exception:  # SQLAlchemy may retain the submitted URL in its exception.
        raise ConfigurationError("invalid PostgreSQL database URL") from None
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+asyncpg")
    if parsed.drivername != "postgresql+asyncpg":
        raise ConfigurationError("database URL must use PostgreSQL with asyncpg")
    if not parsed.database:
        raise ConfigurationError("database URL must select a database")
    return parsed


@dataclass(slots=True)
class PostgresRuntime:
    """Sessions plus table definitions with unambiguous engine ownership.

    ``from_url`` creates and owns an engine. ``from_session_factory`` borrows
    host-managed sessions and never attempts to inspect or dispose their bind.
    """

    session_factory: SessionFactory
    schema: str
    tables: PostgresTables
    _engine: AsyncEngine | None = None
    _owns_engine: bool = False
    _closed: bool = False

    @classmethod
    def from_url(
        cls,
        url: str | URL,
        *,
        schema: str = DEFAULT_SCHEMA,
        engine_options: dict[str, Any] | None = None,
    ) -> "PostgresRuntime":
        schema = validate_schema_name(schema)
        options = dict(engine_options or {})
        options.setdefault("pool_pre_ping", True)
        engine = create_async_engine(_asyncpg_url(url), **options)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        return cls(
            session_factory=factory,
            schema=schema,
            tables=tables_for_schema(schema),
            _engine=engine,
            _owns_engine=True,
        )

    @classmethod
    def from_session_factory(
        cls,
        session_factory: SessionFactory,
        *,
        schema: str = DEFAULT_SCHEMA,
    ) -> "PostgresRuntime":
        if not callable(session_factory):
            raise ConfigurationError("session_factory must be callable")
        schema = validate_schema_name(schema)
        return cls(
            session_factory=session_factory,
            schema=schema,
            tables=tables_for_schema(schema),
        )

    @property
    def owns_engine(self) -> bool:
        return self._owns_engine

    @property
    def engine(self) -> AsyncEngine | None:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._closed:
            raise RuntimeError("PostgresRuntime is closed")
        candidate = self.session_factory()
        async with candidate as session:
            if not isinstance(session, AsyncSession):
                raise ConfigurationError(
                    "session_factory must yield a SQLAlchemy AsyncSession"
                )
            yield session

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_engine and self._engine is not None:
            await self._engine.dispose()

    async def __aenter__(self) -> "PostgresRuntime":
        if self._closed:
            raise RuntimeError("PostgresRuntime is closed")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


__all__ = ["PostgresRuntime", "SessionFactory"]
