"""Normalization for the encrypted virtual Markdown tree."""

from __future__ import annotations

import unicodedata

from .errors import InvalidMemoryPath

_MAX_PATH_BYTES = 4096
_MAX_SEGMENT_BYTES = 255


def normalize_memory_path(path: str, *, allow_root: bool = True) -> str:
    """Return one NFC-normalized absolute POSIX path without traversal."""
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise InvalidMemoryPath("invalid memory path")
    normalized = unicodedata.normalize("NFC", path)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if normalized == "/":
        if allow_root:
            return "/"
        raise InvalidMemoryPath("invalid memory path")
    if normalized.endswith("/"):
        raise InvalidMemoryPath("invalid memory path")
    parts = normalized.split("/")[1:]
    if any(
        not segment
        or segment in {".", ".."}
        or len(segment.encode("utf-8")) > _MAX_SEGMENT_BYTES
        for segment in parts
    ):
        raise InvalidMemoryPath("invalid memory path")
    result = "/" + "/".join(parts)
    if len(result.encode("utf-8")) > _MAX_PATH_BYTES:
        raise InvalidMemoryPath("invalid memory path")
    return result
