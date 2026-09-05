"""Lossless bounded reads, including continuation through a very long line."""

from ..application.queries import integer
from ..domain.models import DocumentSnapshot
from .payloads import ReadPayload, document_payload


def read_window(
    snapshot: DocumentSnapshot,
    *,
    start_line: int = 1,
    max_lines: int = 200,
    start_column: int = 1,
) -> ReadPayload:
    integer(start_line, "start_line", 1, 2147483647)
    integer(max_lines, "max_lines", 1, 2000)
    integer(start_column, "start_column", 1, 1048577)
    lines = snapshot.content.splitlines(keepends=True)
    if start_line > len(lines) + 1:
        raise ValueError(
            "start_line is past the document; use total_lines from a previous read"
        )
    if start_line == len(lines) + 1 and start_column != 1:
        raise ValueError("start_column is past the selected line")
    if start_line <= len(lines) and start_column > len(lines[start_line - 1]):
        raise ValueError("start_column is past the selected line")
    parts, remaining = [], 20000
    next_line, next_column = None, None
    for i in range(start_line - 1, min(len(lines), start_line - 1 + max_lines)):
        column = start_column - 1 if i == start_line - 1 else 0
        text = lines[i][column:]
        parts.append(text[:remaining])
        if len(text) > remaining:
            next_line, next_column = i + 1, column + remaining + 1
            break
        remaining -= len(text)
        if i + 1 < len(lines):
            next_line, next_column = i + 2, 1
        else:
            next_line, next_column = None, None
        if not remaining:
            break
    return {
        **document_payload(snapshot),
        "content": "".join(parts),
        "start_line": start_line,
        "start_column": start_column,
        "total_lines": len(lines),
        "truncated": next_line is not None,
        "next_start_line": next_line,
        "next_start_column": next_column,
    }
