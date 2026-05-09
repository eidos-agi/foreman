# Foreman Control Plane

Portable coordination layer for Foreman jobs.

The first implementation currently lives in the CLI runtime and is exposed as:

- `foreman control-init`
- `foreman control-submit`
- `foreman control-jobs`
- `foreman control-status`

The control plane owns the durable SQLite state model for:

- jobs
- leases
- events
- worker ids
- approvals, later
- API/web surfaces, later

Deployment target:

- local Mac first, using `FOREMAN_HOME=~/.foreman`
- Railway later, using `FOREMAN_HOME` on a persistent volume

It should not run arbitrary worker commands itself when deployed publicly. Worker hosts should lease jobs through a Foreman agent.
