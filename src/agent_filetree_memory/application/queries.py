"""Bounded discovery over encrypted manifests; never materialize plaintext files."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import time
import unicodedata
from typing import TYPE_CHECKING, Any
from collections.abc import AsyncIterator

import regex

from ..domain.models import (
    DocumentSnapshot,
    MemoryAction,
    MemoryEntry,
    VerifiedInvocation,
)
from ..domain.errors import NotFoundOrDenied
from ..domain.paths import normalize_memory_path

MAX_SCAN_ENTRIES = 1000
MAX_SCAN_DIRECTORIES = 100
MAX_SCAN_DOCUMENTS = 200
MAX_SCAN_BYTES = 2 * 1024 * 1024
MAX_RESULT_CHARS = 20000

if TYPE_CHECKING:
    from .service import MemoryService


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range")
    return value


def glob_pattern(pattern: str) -> str:
    if (
        not isinstance(pattern, str)
        or not pattern
        or len(pattern) > 1024
        or any(c in pattern for c in ("\x00", "\\", "{", "}"))
        or pattern.startswith("/")
        or any(p in {"", ".", ".."} for p in pattern.split("/"))
    ):
        raise ValueError(
            "glob must be a relative pattern using *, ?, [], or ** path segments"
        )
    return unicodedata.normalize("NFC", pattern)


def path_matches(path: str, pattern: str) -> bool:
    """Shell-style segment matching; ** matches zero or more complete segments."""
    parts, patterns = path.split("/"), pattern.split("/")
    # Dynamic programming avoids exponential recursion on repeated ** segments.
    reachable = {0}
    for token in patterns:
        if token == "**":
            reachable = (
                set(range(min(reachable), len(parts) + 1)) if reachable else set()
            )
        else:
            reachable = {
                i + 1
                for i in reachable
                if i < len(parts) and fnmatchcase(parts[i], token)
            }
    return len(parts) in reachable


@dataclass
class Scan:
    entries: int = 0
    directories: int = 0
    documents: int = 0
    bytes: int = 0
    reasons: list[str] = field(default_factory=list)
    deadline: float = field(default_factory=lambda: time.monotonic() + 5)
    document: DocumentSnapshot | None = None


async def documents(
    service: MemoryService,
    invocation: VerifiedInvocation,
    path: str,
    scan: Scan,
    *,
    allow_file: bool = False,
) -> AsyncIterator[MemoryEntry]:
    stack = [path]
    while stack:
        if (
            scan.directories >= MAX_SCAN_DIRECTORIES
            or time.monotonic() >= scan.deadline
        ):
            scan.reasons.append("scan_limit")
            return
        directory = stack.pop()
        scan.directories += 1
        try:
            entries = sorted(
                await service.list(invocation, directory), key=lambda e: e.path
            )
        except NotFoundOrDenied:
            if not allow_file or directory != path or scan.directories != 1:
                raise
            # The port's list operation intentionally does not distinguish files
            # from unavailable paths. A read-authorized search may try the file.
            scan.document = await service.read(invocation, path)
            doc = scan.document
            yield MemoryEntry(
                path.rsplit("/", 1)[-1],
                path,
                "document",
                doc.version,
                doc.version_created_at,
            )
            return
        children = []
        for entry in entries:
            if scan.entries >= MAX_SCAN_ENTRIES:
                scan.reasons.append("scan_limit")
                return
            scan.entries += 1
            if entry.kind == "directory":
                children.append(entry.path)
            else:
                yield entry
        stack.extend(reversed(children))


async def glob_documents(
    service: MemoryService,
    invocation: VerifiedInvocation,
    pattern: str,
    *,
    path: str = "/",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    service._authorize(invocation, MemoryAction.LIST)
    root = normalize_memory_path(path)
    pattern = glob_pattern(pattern)
    integer(limit, "limit", 1, 200)
    integer(offset, "offset", 0, 10000)
    scan, paths, seen, chars = Scan(), [], 0, 0
    async for entry in documents(service, invocation, root, scan):
        if not path_matches(entry.path[len(root.rstrip("/")) + 1 :], pattern):
            continue
        seen += 1
        if seen <= offset:
            continue
        if len(paths) >= limit or chars + len(entry.path) > MAX_RESULT_CHARS:
            scan.reasons.append("result_limit")
            break
        paths.append(entry.path)
        chars += len(entry.path)
    return {
        "path": root,
        "paths": paths,
        "truncated": bool(scan.reasons),
        "limit_reasons": scan.reasons,
        "next_offset": offset + len(paths) if "result_limit" in scan.reasons else None,
        "scanned_entries": scan.entries,
    }


def snippet(text: str, column: int = 0) -> dict[str, Any]:
    start = max(0, column - 120)
    return {
        "text": text[start : start + 500],
        "start_column": start + 1,
        "truncated": start > 0 or len(text) > start + 500,
    }


async def grep_documents(
    service: MemoryService,
    invocation: VerifiedInvocation,
    pattern: str,
    *,
    path: str = "/",
    glob: str = "**/*",
    literal: bool = True,
    case_sensitive: bool = True,
    output_mode: str = "content",
    context_lines: int = 1,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    service._authorize(invocation, MemoryAction.READ)
    service._authorize(invocation, MemoryAction.LIST)
    root = normalize_memory_path(path)
    glob = glob_pattern(glob)
    pattern = service._validate_content(pattern, max_bytes=1024, allow_empty=False)
    if "\n" in pattern or "\r" in pattern:
        raise ValueError("pattern must match within a single line")
    if type(literal) is not bool or type(case_sensitive) is not bool:
        raise ValueError("literal and case_sensitive must be booleans")
    if output_mode not in ("content", "files_with_matches"):
        raise ValueError("output_mode must be content or files_with_matches")
    integer(context_lines, "context_lines", 0, 3)
    integer(limit, "limit", 1, 200)
    integer(offset, "offset", 0, 10000)
    try:
        compiled = regex.compile(
            regex.escape(pattern) if literal else pattern,
            flags=0 if case_sensitive else regex.IGNORECASE,
        )
    except (regex.error, OverflowError, RecursionError):
        raise ValueError(
            "invalid regular expression; correct the pattern or set literal=true"
        ) from None
    scan, matches, seen, chars = Scan(), [], 0, 0
    async for entry in documents(service, invocation, root, scan, allow_file=True):
        relative = (
            entry.name
            if entry.path == root
            else entry.path[len(root.rstrip("/")) + 1 :]
        )
        if not path_matches(relative, glob):
            continue
        if scan.documents >= MAX_SCAN_DOCUMENTS:
            scan.reasons.append("scan_limit")
            break
        snapshot = scan.document or await service.read(invocation, entry.path)
        scan.documents += 1
        scan.bytes += len(snapshot.content.encode("utf-8"))
        if scan.bytes > MAX_SCAN_BYTES:
            scan.reasons.append("scan_limit")
            break
        lines = snapshot.content.splitlines()
        for index, line in enumerate(lines):
            if time.monotonic() >= scan.deadline:
                scan.reasons.append("scan_limit")
                break
            try:
                found = compiled.search(line, timeout=0.02)
            except TimeoutError:
                raise ValueError(
                    "regular expression exceeded its time limit; simplify it or set literal=true"
                ) from None
            if found is None:
                continue
            seen += 1
            if seen <= offset:
                if output_mode == "files_with_matches":
                    break
                continue
            item = {"path": snapshot.path, "version": snapshot.version}
            if output_mode == "content":
                item.update(
                    {
                        "line_number": index + 1,
                        **snippet(line, found.start()),
                        "context": [
                            {"line_number": n + 1, **snippet(lines[n])}
                            for n in range(
                                max(0, index - context_lines),
                                min(len(lines), index + context_lines + 1),
                            )
                            if n != index
                        ],
                    }
                )
            # Bound strings in the entire result, including paths repeated in each match.
            cost = len(str(item))
            if len(matches) >= limit or chars + cost > MAX_RESULT_CHARS:
                scan.reasons.append("result_limit")
                break
            matches.append(item)
            chars += cost
            if output_mode == "files_with_matches":
                break
        if scan.reasons:
            break
    return {
        "path": root,
        "matches": matches,
        "truncated": bool(scan.reasons),
        "limit_reasons": scan.reasons,
        "next_offset": offset + len(matches)
        if "result_limit" in scan.reasons
        else None,
        "scanned_documents": scan.documents,
        "scanned_bytes": scan.bytes,
    }
