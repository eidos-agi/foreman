# Worker Agent

The worker agent is the host-side daemon in the Foreman topology. Its only responsibility is to take control-plane jobs and run them through the local Foreman CLI runtime. It has no opinion about what the job does, what engine it invokes, or what product the output is for. Foreman is generic infrastructure; the worker agent is generic worker-host plumbing.

## Topology

```
+---------------------+         outbound poll/lease         +---------------------------+
|  Control plane      |  <--------------------------------- |  Worker agent (host A)    |
|  (foreman-control)  |                                     |  packages/foreman-agent   |
|                     |                                     |  -> foreman-cli runtime   |
|  - jobs queue       |  ---------------------------------> |                           |
|  - leases           |          job payload + lease        +---------------------------+
|  - status events    |
|                     |         outbound poll/lease         +---------------------------+
|                     |  <--------------------------------- |  Worker agent (host B,    |
|                     |                                     |  e.g. rentamac)           |
|                     |  ---------------------------------> |  -> foreman-cli runtime   |
+---------------------+          job payload + lease        +---------------------------+
```

All arrows are initiated by the worker. The control plane never needs inbound SSH, VPN, or port forwarding into a worker host.

## Outbound-only contract

The worker agent must be deployable to a personal Mac, a remote rented Mac, or any host where opening inbound network access is undesirable. To make that safe:

- The agent dials the control plane. The control plane does not dial the agent.
- The agent identifies itself with an `--agent-id` (defaulting to `hostname:pid`). Jobs targeted at a specific host advertise that host's id; untargeted jobs may be leased by any agent.
- The agent only reads jobs it can lease, and only writes status, logs, and results for jobs it currently holds a lease on.
- Transport is the control plane's concern. SQLite-on-shared-disk, HTTP polling, or a future push channel are all valid, as long as the worker initiates the connection.

## Lease lifecycle

Each unit of work moves through the same lifecycle on the agent:

1. **Acquire**: the agent calls into the control plane and atomically claims one pending job by setting an exclusive lease keyed on `agent_id` with a TTL (`--lease-sec`).
2. **Dispatch**: the agent hands the job to the local Foreman CLI runtime via the `agent-run` command path. The CLI creates the worktree, builds the engine command, and spawns the worker process.
3. **Heartbeat / wait**: while the lease is held, the agent either returns immediately (fire-and-forget, lease expires naturally when the worker finishes) or waits for terminal worker status (`--wait`).
4. **Report**: terminal status from the worker is reflected back as a control-plane status (`succeeded`, `failed`, `interrupted`, etc.) plus event log entries.
5. **Release**: on completion, idle, or shutdown, the lease is released so another agent can pick up subsequent jobs.

If an agent dies mid-job, the lease TTL guarantees the job becomes leasable again; another agent (or the same agent on restart) can pick it up. Leases are not promises about success — they are promises about exclusive ownership during a bounded window.

## CLI execution boundary

The worker agent does not reimplement worktree creation, engine selection, or worker process management. Those belong to `packages/foreman-cli`. The boundary is:

- **Worker agent** decides *whether* to run a job here, *now*, under this `agent_id`.
- **Foreman CLI** decides *how* to run a job — which engine, which worktree, which arguments, how to capture logs and results.

The package-local `packages/foreman-agent/scripts/agent.py` is intentionally a thin wrapper that forwards to the CLI's `agent-run` subcommand. Adding business logic to the agent script is a smell; that logic belongs in the CLI runtime so every entrypoint (root compatibility wrapper, agent script, future daemon supervisor) shares one code path.

## Multi-host operation

Foreman expects more than one worker host over time:

- the developer's local Mac, for fast inner-loop work
- a dedicated personal-infra Mac (for example, `rentamac`) for long-running CLI engines that should not block the developer's machine
- additional Macs or sandboxes as capacity grows

Each host runs its own worker agent process. Hosts are differentiated by `--agent-id`. Job routing is the control plane's job: it can target a specific `agent_id`, fan out to the first available agent, or pin certain engine types to certain hosts.

## What the worker agent must not do

To keep Foreman generic and hosts safe:

- It must not embed product-specific behavior, prompt content, or domain logic. Those belong in the engine layer or in caller systems that submit jobs.
- It must not accept inbound connections on the host. All transport is outbound from the agent.
- It must not duplicate CLI runtime logic. New behavior (different engines, new worker types, richer status) lands in `foreman-cli` first and is exposed through `agent-run`.
- It must not assume the control plane is local. Any path-based or filesystem-shared coordination must keep working when the control plane is moved off-box.
