from datetime import datetime, timezone

import pytest

from agent_filetree_memory.application.queries import path_matches
from agent_filetree_memory.domain.errors import EditConflict, QuotaExceeded
from agent_filetree_memory.domain.models import DocumentSnapshot
from agent_filetree_memory.domain.text import replace_text
from agent_filetree_memory.mcp.reading import read_window


def snapshot(content):
    now = datetime.now(timezone.utc)
    return DocumentSnapshot("/notes.md", content, 1, now, now)


@pytest.mark.parametrize(
    "content",
    ["", "x", "a\r\nb\r\n", "café\n" * 800, "x" * 60001 + "\nend\n", "a\n" * 2501],
)
def test_read_pages_reconstruct_every_byte(content):
    doc = snapshot(content)
    line, column, pieces = 1, 1, []
    for _ in range(100):
        page = read_window(doc, start_line=line, start_column=column)
        assert len(page["content"]) <= 20000
        pieces.append(page["content"])
        if not page["truncated"]:
            break
        line, column = page["next_start_line"], page["next_start_column"]
    else:
        pytest.fail("read pagination did not terminate")
    assert "".join(pieces) == content


def test_read_eof_and_invalid_offsets():
    assert read_window(snapshot("a\nb\n"), start_line=3)["content"] == ""
    for kwargs in (
        {"start_line": 4},
        {"start_line": True},
        {"max_lines": 0},
        {"start_column": 3},
        {"start_line": 3, "start_column": 2},
    ):
        with pytest.raises(ValueError):
            read_window(snapshot("a\nb\n"), **kwargs)


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("notes.md", "**/*.md", True),
        ("a/b/notes.md", "**/*.md", True),
        ("a/b.md", "*.md", False),
        ("a/b.md", "a/?.md", True),
        ("a/b/c.md", "a/*.md", False),
        ("a/B.md", "a/[ab].md", False),
        ("a/b.md", "**/**/**/b.md", True),
        ("a/b.md", "**/x/**", False),
    ],
)
def test_glob_segment_semantics(path, pattern, expected):
    assert path_matches(path, pattern) is expected


def test_exact_edit_preserves_whitespace_and_handles_deletion():
    text = "# A\r\nvalue: 1\r\n\r\n# B\r\nvalue: 1\r\n"
    result = replace_text(
        text, "# B\r\nvalue: 1", "# B\r\nvalue: 2", replace_all=False, max_bytes=200
    )
    assert result == text.replace("# B\r\nvalue: 1", "# B\r\nvalue: 2")
    assert replace_text("a$b", "$", "", replace_all=False, max_bytes=10) == "ab"


@pytest.mark.parametrize("old,new", [("missing", "x"), ("a", "x"), ("aa", "aa")])
def test_edit_rejects_missing_ambiguous_and_noop(old, new):
    with pytest.raises(EditConflict):
        replace_text("aa", old, new, replace_all=False, max_bytes=20)


def test_replace_all_is_nonoverlapping_and_checks_growth_before_allocation():
    assert replace_text("aaa", "aa", "b", replace_all=True, max_bytes=10) == "ba"
    with pytest.raises(QuotaExceeded):
        replace_text(
            "a" * 100000, "a", "é" * 10000, replace_all=True, max_bytes=1048576
        )
