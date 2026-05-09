# Foreman Agent

Worker-host daemon for leasing Foreman control-plane jobs and executing them through the local Foreman CLI runtime.

The first implementation currently lives in the CLI runtime and is exposed as:

- `foreman agent-run --once`
- `foreman agent-run --wait --agent-id <host>`

Target hosts:

- this Mac for local development
- `rentamac` or another personal-infra Mac for Claude/Codex/Gemini CLI execution

The agent should poll or subscribe outbound to the control plane. The control plane should not need inbound SSH access to worker machines.
