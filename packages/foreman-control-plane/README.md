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
- `foreman control-serve [--host 127.0.0.1] [--port 53640] [--quiet] [--once-smoke]`
- `foreman control-hint [--host 127.0.0.1] [--port 53640]`

Package form (use when invoking the control plane as a standalone unit):

- `python3 packages/foreman-control-plane/scripts/control.py init`
- `python3 packages/foreman-control-plane/scripts/control.py submit --repo <path> --engine <engine> "<spec>"`
- `python3 packages/foreman-control-plane/scripts/control.py jobs [--limit N]`
- `python3 packages/foreman-control-plane/scripts/control.py status <job_id>`
- `python3 packages/foreman-control-plane/scripts/control.py serve [--host 127.0.0.1] [--port 53640] [--quiet] [--once-smoke]`
- `python3 packages/foreman-control-plane/scripts/control.py hint [--host 127.0.0.1] [--port 53640]`

Both forms write to the same SQLite database under `FOREMAN_HOME` and are interchangeable.

## HTTP API (`serve`)

`control-serve` exposes a small JSON HTTP API backed by the same SQLite state. It records and reads jobs; it does not run them.

- `GET /api/health` -> `{ok, state_home, sqlite_path, pid}`
- `GET /api/control/jobs?limit=N` -> same shape as `control-jobs`
- `GET /api/control/jobs/<job_id>` -> same shape as `control-status` (includes events)
- `POST /api/control/jobs` -> JSON body matching `control-submit` fields (`repo_path` or `repo`, `engine`, `spec`, `base_ref`, `test_command`, `timeout_sec`, `caller`, `parent`, `run_id`, `contract`, `allow_dirty`); returns the same payload as `submit_control_job`

`--once-smoke` binds the socket, prints a JSON status payload (`url`, `host`, `port`, `status`, `state_home`, `sqlite_path`, `pid`), and exits successfully. It is the supported way for tests to confirm argument parsing and binding without leaving a process running.

`control-hint` is the discoverability command for clients. It reuses a healthy service when `FOREMAN_HOME/control-plane.json` points at one, starts `control-serve` when needed, and returns the live URL plus health and state-file metadata.

Example:

```sh
# start the API on a fixed port
python3 packages/foreman-control-plane/scripts/control.py serve --port 53640

# health check
curl -s http://127.0.0.1:53640/api/health

# submit a job
curl -s -X POST -H 'Content-Type: application/json' \
    -d '{"repo":"~/repos/my-project","engine":"claude","spec":"Add a healthcheck"}' \
    http://127.0.0.1:53640/api/control/jobs
```

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
