"""GhostQA MCP Server — Expose GhostQA as an MCP tool for AI agents.

This package wraps the GhostQA engine as a Model Context Protocol server,
allowing AI agents (Claude Code, Cursor, etc.) to discover and invoke
GhostQA behavioral testing directly from their tool ecosystem.

Transport: stdio (standard for MCP CLI tools).
"""

from ghostqa.mcp.server import create_server, main

__all__ = ["create_server", "main"]
