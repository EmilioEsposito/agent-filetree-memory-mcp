import pytest

from agent_filetree_memory.domain.errors import InvalidMemoryPath
from agent_filetree_memory.domain.paths import normalize_memory_path


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("notes/today.md", "/notes/today.md"),
        ("/notes/today.md", "/notes/today.md"),
        ("/", "/"),
        ("/caf\u0065\u0301.md", "/caf\u00e9.md"),
    ],
)
def test_normalizes_virtual_paths(source: str, expected: str) -> None:
    assert normalize_memory_path(source) == expected


@pytest.mark.parametrize(
    "source",
    ["", "/a//b", "/a/../b", "/a/./b", "a\\b", "/a/", "\x00"],
)
def test_rejects_ambiguous_or_unsafe_paths(source: str) -> None:
    with pytest.raises(InvalidMemoryPath, match="invalid memory path"):
        normalize_memory_path(source)


def test_can_reject_root_for_document_operations() -> None:
    with pytest.raises(InvalidMemoryPath):
        normalize_memory_path("/", allow_root=False)
