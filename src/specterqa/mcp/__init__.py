"""SpecterQA MCP Server — Expose SpecterQA as an MCP tool for AI agents.

This package wraps the SpecterQA engine as a Model Context Protocol server,
allowing AI agents (Claude Code, Cursor, etc.) to discover and invoke
SpecterQA behavioral testing directly from their tool ecosystem.

Transport: stdio (standard for MCP CLI tools).
"""

from specterqa.mcp.server import create_server, main

__all__ = ["create_server", "main"]
