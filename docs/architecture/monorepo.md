# Foreman Monorepo

Foreman is now organized as a small monorepo so the reusable runtime, control plane, worker agent, MCP plugin, and future integrations can share one contract without becoming separate drifting repos.

Package map:

- `packages/foreman-cli`: local worktree and engine execution runtime.
- `packages/foreman-control-plane`: portable SQLite-backed job coordination model.
- `packages/foreman-agent`: worker-host daemon that leases jobs and runs local CLIs.
- `packages/foreman-mcp`: Codex MCP/plugin shim.
- `packages/foreman-sdk`: future shared schemas and client helpers.
- `integrations/slack`: future generic Slack transport adapter.

Compatibility:

- `scripts/foreman.py` remains the stable CLI entrypoint.
- `scripts/mcp_server.py` remains the stable MCP entrypoint.
- Current Codex plugin configuration should keep working while implementation moves under `packages/`.

Deployment direction:

- local all-in-one first
- Railway-hosted control plane later
- `rentamac` or other personal-infra Macs as worker agents
