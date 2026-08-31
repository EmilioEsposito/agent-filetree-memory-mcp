"""Package metadata stays aligned with the public runtime API."""

from importlib.metadata import version

from agent_filetree_memory import __version__


def test_runtime_version_matches_distribution_metadata() -> None:
    assert __version__ == version("agent-filetree-memory-mcp")
