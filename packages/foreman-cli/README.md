# Foreman CLI

Local execution runtime for Foreman.

Owns:

- git worktree creation
- engine command construction
- worker process lifecycle
- local monitor daemon
- worker logs and result files
- compatibility with the historical `scripts/foreman.py` entrypoint

The root `scripts/foreman.py` file is a compatibility wrapper. New implementation work should happen under this package unless it belongs to the control plane, agent, or MCP package.
