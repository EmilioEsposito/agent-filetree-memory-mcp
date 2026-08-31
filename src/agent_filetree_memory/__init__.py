"""Encrypted, capability-scoped file-tree memory for agents."""

from .domain.models import (
    DeleteResult,
    DocumentSnapshot,
    MemoryAction,
    MemoryEntry,
    Scope,
    VerifiedInvocation,
    WriteResult,
)

__all__ = [
    "DeleteResult",
    "DocumentSnapshot",
    "MemoryAction",
    "MemoryEntry",
    "Scope",
    "VerifiedInvocation",
    "WriteResult",
]

__version__ = "0.3.0"
