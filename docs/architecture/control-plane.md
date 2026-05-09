# Foreman Control Plane Architecture

The control plane is the durable, generic coordination layer for Foreman work. It records what jobs exist, who is allowed to run them, and what happened to them. It does not execute work itself.

This separation is the load-bearing design choice in Foreman. Everything else in the system follows from it.

## Goals

- Be generic. The control plane has no opinions about engines, repos, or product domains.
- Be portable. The state model is a single SQLite file under `FOREMAN_HOME` that can move between a developer Mac and a hosted service without schema changes.
- Be safe to host. A hosted instance must not need to spawn arbitrary engine processes or hold credentials that belong to worker machines.

## State model

The control plane is the schema, not the runner. The current implementation lives in the CLI runtime and is the source of truth for the layout below; this document describes its contract.

### `control_jobs`

A row per submitted job, used by both submitters and lease holders.

- `id`: opaque job id (`job-YYYYMMDD-HHMMSS-<hex>`)
- `status`: `pending`, `leased`, `running`, `succeeded`, `failed`
- `repo_path`, `engine`, `base_ref`, `spec`, `test_command`, `timeout_sec`
- `caller`, `parent`, `run_id`, `contract`: provenance and grouping
- `allow_dirty`: whether the runner may operate on a non-clean working tree
- `lease_owner`, `lease_expires_at`, `leased_at`: lease state
- `created_at`, plus terminal timestamps recorded as events

The fields are intentionally generic. There is no engine-specific column, no domain column, and no bot column. A job is "run this spec on this repo with this engine, using this contract." Anything beyond that belongs in the spec or in the contract file the spec points at.

### Leases

A worker takes a job by claiming a lease:

- pending or expired-leased rows are eligible
- the worker writes its `agent_id` into `lease_owner`
- `lease_expires_at` is set to `now + lease_sec`
- the transition is atomic via `BEGIN IMMEDIATE` and a single SQLite transaction

Lease expiration is a recovery mechanism, not a scheduling primitive. If a worker dies the row becomes leasable again after its lease expires, and another worker can pick it up.

### Events

`control_events` is an append-only audit log keyed by job id:

- `submitted`, `leased`, `started`, `succeeded`, `failed`, plus free-form notes
- each event carries a JSON payload for context (lease holder, exit codes, worker id)
- events are how tools reconstruct what happened without trusting the current row state

Events are cheap. The control plane writes them generously and never edits them.

### Worker ids and run ids

Worker ids identify a host or process taking jobs. Run ids group jobs that belong to the same higher-level intent (a multi-worker run, a planned milestone, a chained delegation).

Both are opaque strings to the control plane. Meaning is assigned by callers; the control plane only stores and indexes them.

## Why execution stays in agents

The control plane stores state. Agents do work. The split is not cosmetic.

- **Security.** A hosted control plane should not hold the credentials needed to run engine CLIs, push to git remotes, or write to a developer's filesystem. Keeping execution out of the control plane means the deployment surface that is exposed to the network is the smallest possible: a SQLite-backed coordinator with append-only events.
- **Trust boundaries.** Worker machines own their own engine binaries, their own git config, their own ssh keys, and their own filesystem. The control plane does not need any of those. It hands a worker a row and the worker decides how to run it.
- **Deployability.** Because the control plane never spawns arbitrary processes, it can run on a small Railway service with a persistent volume. Workers can run on a personal Mac, a `rentamac` host, or any machine that can reach the control plane outbound.
- **Failure isolation.** If a worker crashes, its lease expires and another worker can take the job. If the control plane restarts, in-flight rows stay pending or leased and resume cleanly. No single component is a fan-in for the others' failure modes.
- **Genericness.** The control plane has nothing to say about what an engine is or what a spec means. That keeps it reusable across domains. Caller-owned behavior belongs in the system that submits jobs or in the worker that runs them, not here.

## Connectivity model

The control plane is an outbound target, not an inbound orchestrator.

- callers submit jobs to the control plane
- workers poll or subscribe outbound and lease jobs
- the control plane never opens a connection back to a worker

This is what allows worker hosts to live behind NAT, on personal infrastructure, or on intermittently-online machines without any inbound exposure.

## What does not belong in the control plane

- engine command construction
- worktree creation, cleanup, or git operations
- any spawn of subprocesses other than itself
- any caller-domain or bot schema
- any code path that needs credentials for an external system other than its own database
