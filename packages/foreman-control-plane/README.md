# Foreman Control Plane

Portable coordination layer for Foreman jobs.

The control plane owns the durable SQLite state model. It does not run worker commands. Worker hosts lease jobs through a Foreman agent and execute them through the Foreman CLI runtime.

## What it owns

- `control_jobs` table: pending, leased, running, succeeded, failed
- leases: `lease_owner`, `lease_expires_at`, `leased_at`
- events: append-only audit trail per job
- worker ids and run ids
- approvals, later
- API and web surfaces, later

## Surface

The first implementation lives in the Foreman CLI runtime. The control-plane package exposes a thin executable wrapper that forwards to the CLI without re-implementing the state model.

CLI form (compatibility, still supported):

- `foreman control-init`
- `foreman control-submit`
- `foreman control-jobs`
- `foreman control-status <job_id>`

Package form (use when invoking the control plane as a standalone unit):

- `python3 packages/foreman-control-plane/scripts/control.py init`
- `python3 packages/foreman-control-plane/scripts/control.py submit --repo <path> --engine <engine> "<spec>"`
- `python3 packages/foreman-control-plane/scripts/control.py jobs [--limit N]`
- `python3 packages/foreman-control-plane/scripts/control.py status <job_id>`

Both forms write to the same SQLite database under `FOREMAN_HOME` and are interchangeable.

## Local usage

```sh
# initialize the SQLite state bundle under ~/.foreman
python3 packages/foreman-control-plane/scripts/control.py init

# submit a job
python3 packages/foreman-control-plane/scripts/control.py submit \
    --repo ~/repos/my-project \
    --engine claude \
    "Add a healthcheck endpoint"

# list recent jobs
python3 packages/foreman-control-plane/scripts/control.py jobs --limit 20

# inspect a single job (includes events)
python3 packages/foreman-control-plane/scripts/control.py status job-20260508-abcdef
```

A separate Foreman agent (`foreman agent-run --once` or `--wait`) leases pending jobs and runs them through the CLI runtime.

## Future Railway usage

The same wrapper is the deployable entrypoint for a hosted control plane:

- run the wrapper as the Railway service start command
- mount a persistent volume and set `FOREMAN_HOME=/data/foreman`
- the SQLite file at `$FOREMAN_HOME/foreman.sqlite3` is the durable state bundle
- worker Macs run the Foreman agent and reach the control plane outbound; the control plane never needs inbound SSH into a worker
- when running publicly, the control plane process must not execute arbitrary worker commands itself; it only records jobs, leases, and events

The package boundary is what makes this swap possible: the control plane can be deployed without dragging the rest of the runtime, and worker hosts can ship their own image that includes only the agent and the CLI runtime.
