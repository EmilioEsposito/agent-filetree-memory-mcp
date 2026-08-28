"""Optional MCP transport adapter.

Install the ``mcp`` extra for headless tools or the ``app`` extra for the
current-capability browser and editor.
"""

from .server import create_mcp_server

__all__ = ["create_mcp_server"]
