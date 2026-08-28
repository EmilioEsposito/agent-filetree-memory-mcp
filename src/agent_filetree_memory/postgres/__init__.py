"""Optional PostgreSQL persistence adapter.

Importing this module performs no environment reads, engine creation, DDL, or
network access. Hosts opt into either a static URL runtime or a borrowed async
session factory explicitly.
"""

from .runtime import PostgresRuntime, SessionFactory
from .janitor import JanitorReport, PostgresJanitor
from .schema import (
    DEFAULT_SCHEMA,
    PostgresTables,
    tables_for_schema,
    validate_schema_name,
)
from .store import EnvelopeCodec, PostgresMemoryStore, PostgresStoreConfig

__all__ = [
    "DEFAULT_SCHEMA",
    "EnvelopeCodec",
    "JanitorReport",
    "PostgresMemoryStore",
    "PostgresJanitor",
    "PostgresRuntime",
    "PostgresStoreConfig",
    "PostgresTables",
    "SessionFactory",
    "tables_for_schema",
    "validate_schema_name",
]
