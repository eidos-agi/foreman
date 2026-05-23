# Foreman Agent

Worker-host daemon for leasing Foreman control-plane jobs and executing them through the local Foreman CLI runtime.

The agent owns no scheduling logic of its own. It is an outbound poller that asks the control plane for work, takes a lease, hands the job to the local Foreman CLI runtime, and reports back. All execution lives inside `packages/foreman-cli`; this package is only the host-side entrypoint.

## Surface

The CLI runtime currently exposes the agent loop as:

- `foreman agent-run --once`
- `foreman agent-run --wait --agent-id <host>`

This package ships a package-local entrypoint that forwards to that command without duplicating business logic:

- `packages/foreman-agent/scripts/agent.py`

It accepts the same flags as `foreman agent-run` (`--once`, `--wait`, `--agent-id`, `--lease-sec`, `--poll-interval-sec`, `--idle-sleep-sec`).

## Local Mac usage

On the Mac running the control plane and CLI, run a single lease-and-execute pass:

```bash
python3 packages/foreman-agent/scripts/agent.py --once --wait
```

Run a long-lived agent that keeps polling and runs jobs as they appear:

```bash
python3 packages/foreman-agent/scripts/agent.py \
  --agent-id "$(hostname)" \
  --lease-sec 300 \
  --idle-sleep-sec 5
```

`--once` returns after the first lease attempt (idle or completed). Without `--once`, the agent loops forever; stop it with Ctrl-C or by killing the process.

## Future rentamac usage

`rentamac` (or any other personal-infra Mac dedicated to running CLI engines) is the intended second host. The shape is the same; only the location of the control plane changes. On that host:

```bash
git clone <foreman-repo> ~/foreman
cd ~/foreman
python3 packages/foreman-agent/scripts/agent.py \
  --agent-id rentamac \
  --lease-sec 600
```

When the control plane moves off-box (Railway, etc.), the agent connects outbound to it from `rentamac` using whatever transport the control-plane package exposes. The control plane never needs inbound SSH, VPN, or port-forwarding to the worker host. Each worker advertises an `--agent-id` so jobs targeted at a specific host (for example, a host with a particular CLI engine installed) only get leased there.

## Boundaries

This package does not:

- decide which jobs to run (that is the control plane)
- create worktrees or spawn engine processes (that is `foreman-cli`)
- speak any product-specific protocol — it is generic worker-host plumbing
