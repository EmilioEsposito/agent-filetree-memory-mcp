"""Encrypted, capability-scoped file-tree memory for agents."""

from importlib.metadata import PackageNotFoundError, version as _distribution_version

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

try:
    __version__ = _distribution_version("agent-filetree-memory-mcp")
except PackageNotFoundError:
    # Source-only imports (for example, `python -S` checks) have no installed
    # distribution metadata. Built and editable installs always use metadata.
    __version__ = "0+unknown"
