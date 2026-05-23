# Foreman MCP

Codex MCP/plugin shim for Foreman.

Owns:

- MCP tool schemas
- argument translation from MCP calls to Foreman CLI commands
- stable compatibility for Codex plugin users

The root `scripts/mcp_server.py` file is a compatibility wrapper. New MCP implementation work should happen under this package.
