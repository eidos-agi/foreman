# Foreman

![Foreman logo](https://raw.githubusercontent.com/eidos-agi/foreman/main/assets/foreman-rhea-logo.png)

Foreman is an Eidos AGI Codex plugin/runtime for delegating implementation work to AI engineer workers while Codex or another caller remains architect and QA.

It is part of the Eidos AGI plugin family alongside Rhea:

- `rhea@eidos-agi`: sovereign model routing, debate, pairing, and image tools.
- `foreman@eidos-marketplace`: multi-agent coding delegation and git worktree execution.

The plugin exposes MCP tools:

- `foreman_delegate`
- `foreman_list`
- `foreman_status`
- `foreman_control_submit`
- `foreman_control_jobs`
- `foreman_control_hint`
- `foreman_control_status`
- `foreman_agent_run_once`
- `foreman_tail`
- `foreman_collect`
- `foreman_finalize`
- `foreman_note`
- `foreman_interrupt`

The runtime stores state in `~/.foreman/foreman.sqlite3`, logs in `~/.foreman/logs`, and worktrees in `~/.foreman/worktrees`.

## Monorepo Layout

Foreman is now a small monorepo. The stable root scripts remain in place for existing Codex/plugin configuration, but implementation code lives under `packages/`.

```text
packages/
  foreman-cli/            local worktree and engine execution runtime
  foreman-control-plane/  portable SQLite-backed coordination layer
  foreman-agent/          worker-host lease runner for Mac/Linux boxes
  foreman-mcp/            Codex MCP/plugin shim
  foreman-sdk/            future shared schemas and clients
integrations/
  slack/                  future generic Slack transport adapter
docs/
  architecture/           design notes and deployment shape
scripts/
  foreman.py              compatibility wrapper
  mcp_server.py           compatibility wrapper
```

The monorepo boundary matters because `ControlJobSpec`, job states, worker states, event types, leases, and approval policy need to stay synchronized across CLI, MCP, Railway control plane, rented-Mac agents, and generic clients.

## Local Control Plane And Agent

Foreman can now prove the future distributed shape on one computer:

- `foreman control-submit` writes a pending job into the same portable SQLite state bundle.
- `foreman agent-run --once` leases one pending job from that SQLite control plane.
- The agent delegates the leased job through the normal Foreman worker runtime.
- The worker still runs in an isolated git worktree and reports through the existing worker tables, logs, and result files.

That means the first deployment target is just this Mac. Later, the control plane can move to Railway with `FOREMAN_HOME` on a persistent volume, while rented Mac worker hosts run `agent-run` against the same API/SQLite-backed contract.

Local proof path:

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py control-init

python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py control-submit \
  --repo /path/to/repo \
  --engine smoke \
  --allow-dirty \
  --timeout-sec 20 \
  --test-command "true" \
  "No-edit smoke job submitted through the control plane."

python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py control-jobs
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py agent-run --once --wait
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py control-status <job_id>
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py control-hint
```

For an always-on local or cloud-Mac worker, omit `--once`:

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py agent-run --wait --agent-id mac-worker-01
```

The control-plane tables are intentionally generic: jobs, leases, events, worker ids, and errors. Slack, a local web UI, Railway, or another future client should submit structured jobs here instead of learning Foreman's worktree internals. `control-hint` is the local discovery path for that API: it starts or reuses `control-serve`, writes `~/.foreman/control-plane.json`, and returns the live URL.

Supported engines:

- `claude`: Claude Code, default implementation worker.
- `codex`: Codex CLI, stronger reasoning fallback or QA worker.
- `gemini`: Gemini CLI, broad-context alternate worker/reviewer.
- `aider`: Aider, narrow git-oriented patch worker.
- `opencode`: opencode headless worker, useful for alternate agents/models.
- `gemma4`: opencode-backed local Gemma worker, throttled for this Mac.
- `smoke`: deterministic local fake worker for plumbing tests.

Default engine commands:

- `claude -p <prompt>`
- `codex exec --sandbox workspace-write <prompt>`
- `gemini --skip-trust --approval-mode yolo -p <prompt>`
- `aider --yes-always --message <prompt>`
- `opencode run --format json <prompt>`
- `opencode run --model $FOREMAN_GEMMA4_MODEL --format json <prompt>`

## Install In Claude Code

Foreman ships as a Claude Code plugin via the `.claude-plugin/` manifest at
the repo root. Two ways to install:

**1. Local dev / your own checkout:**

```bash
git clone git@github.com:eidos-agi/foreman.git ~/repos-eidos-agi/foreman
claude --plugin-dir ~/repos-eidos-agi/foreman    # single-session use
```

**2. Eidos Marketplace install:**

```bash
claude plugins marketplace add eidos-agi/eidos-marketplace
claude plugins install foreman@eidos-marketplace
```

The plugin exposes:

- All 13 `foreman_*` MCP tools (delegate, list, status, control, etc.)
- The `delegate-with-foreman` skill (auto-loads when you ask Claude to delegate work)
- Inherits the same scripts/runtime as the Codex install

**About the foreman daemon (transparency):**

Foreman runs a small background daemon (`foreman-agent`) to coordinate
worker agents and host the web terminal where you watch them run.
This is intentional — without the daemon you lose the live-monitoring view
that makes parallel agent fleets legible.

- **What it does**: tails worker logs, manages worktree leases, serves the
  web terminal on a local port, exposes status to MCP tools.
- **What it costs**: ~50MB RAM idle, near-zero CPU when nothing's running,
  only spawns worker processes on demand.
- **How to start it**: `foreman daemon start` — explicit, not auto. The
  plugin install does NOT start it for you.
- **How to stop it**: `foreman daemon stop` or just `pkill -f foreman-agent`.

If you want one-shot delegation without the daemon, foreman supports a
no-daemon mode for the simplest cases — but you lose the web terminal.

## Install From PyPI

Foreman is packaged for PyPI as `eidos-foreman` because the plain `foreman`
package name is already owned by another project. The installed command remains
`foreman`.

```bash
pip install eidos-foreman
foreman --help
foreman-mcp
```

For local development:

```bash
pip install -e ".[dev]"
pytest
python -m build
twine check dist/*
```

PyPI publishing uses GitHub trusted publishing. Configure a pending publisher
for project `eidos-foreman`, owner `eidos-agi`, repository `foreman`, workflow
`publish.yml`, and environment `pypi`, then push a `v0.3.1` style tag.

## Install In Codex

Clone the repo into the Eidos workspace:

```bash
mkdir -p /Users/dshanklinbv/repos-eidos-agi
git clone git@github.com:eidos-agi/foreman.git /Users/dshanklinbv/repos-eidos-agi/foreman
```

Install or refresh the Eidos AGI Codex plugin cache:

```bash
mkdir -p /Users/dshanklinbv/.codex/plugins/cache/eidos-agi/foreman/0.3.1
rsync -a --delete --exclude '.git' --exclude '__pycache__' \
  /Users/dshanklinbv/repos-eidos-agi/foreman/ \
  /Users/dshanklinbv/.codex/plugins/cache/eidos-agi/foreman/0.3.1/
```

Add Foreman to the Eidos AGI marketplace at `~/.agents/plugins/marketplace.json`, next to Rhea:

```json
{
  "name": "foreman",
  "source": {
    "source": "local",
    "path": "./plugins/foreman"
  },
  "policy": {
    "installation": "AVAILABLE",
    "authentication": "ON_INSTALL"
  },
  "category": "Productivity"
}
```

Enable the plugin and MCP server in `~/.codex/config.toml`:

```toml
[plugins."foreman@eidos-marketplace"]
enabled = true

[mcp_servers.foreman]
transport = "stdio"
command = "python3"
args = ["/Users/dshanklinbv/repos-eidos-agi/foreman/scripts/mcp_server.py"]
tool_timeout_sec = 1800
```

Restart Codex after editing config. Verify the MCP server:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/mcp_server.py
```

The tools list should include `foreman_delegate`, `foreman_list`, `foreman_status`, `foreman_control_submit`, `foreman_control_jobs`, `foreman_control_hint`, `foreman_control_status`, `foreman_agent_run_once`, `foreman_tail`, `foreman_monitor_hint`, `foreman_collect`, `foreman_finalize`, `foreman_note`, and `foreman_interrupt`.

The expected installed shape mirrors Rhea:

```text
/Users/dshanklinbv/.codex/plugins/cache/eidos-agi/rhea/0.1.0
/Users/dshanklinbv/.codex/plugins/cache/eidos-agi/foreman/0.3.1
```

## Manual Smoke Test

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py delegate \
  --repo /path/to/repo \
  --engine claude \
  --run-id feature-alpha \
  --contract "Public API: core exposes create_game(), available_actions(), apply_action(). UI must not invent alternate commands." \
  --timeout-sec 900 \
  --test-command "python3 -m pytest" \
  "Add a small feature and verify it."

python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py list
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py status <worker_id>
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py watch <worker_id>
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py web <worker_id> --open
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py collect <worker_id>
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py note <worker_id> "Human steering note"
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py interrupt <worker_id> "Going down the wrong path"
```

## Real-Time Monitoring

Use `watch` from a terminal:

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py watch <worker_id>
```

`watch` streams the worker log and status until the worker exits. Pressing `Ctrl-C` stops watching only; it does not cancel the worker.

For MCP/Codex snapshots, use `foreman_status` for a cheap durable handle check and `foreman_tail` for bounded recent logs. MCP request/response calls are not a true streaming terminal, so the MCP server also exposes `foreman_monitor_hint` with the exact terminal command.

`foreman_delegate` is intentionally fire-and-return. Once the worktree exists and the worker process is spawned, the delegate response includes the durable `worker_id`, `run_id`, worktree path, log path, and reattach commands. Do not keep an MCP request open waiting for Claude Code or another worker to finish; come back with `foreman_status`, `foreman_tail`, or `foreman_collect`.

The MCP server is intentionally a stable shim. Operational behavior lives in `scripts/foreman.py`, and the shim shells out to that CLI for each request. That means most Foreman improvements do not require a plugin restart. Restart Codex only when the MCP tool interface changes, such as adding or renaming a tool or changing input schemas.

For a browser cockpit, start the web monitor:

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py web <worker_id> --open
```

`web` attaches to a local Foreman daemon. If the daemon is already healthy, Foreman reuses it. If it is missing or stale, Foreman starts one on Foreman's stable local port, writes `~/.foreman/daemon.json`, and returns a URL like `http://127.0.0.1:53631/?worker=<worker_id>`. Pass `--port 0` only when you explicitly want an ephemeral port.

The daemon is a lightweight multiplexer over the same durable Foreman state: worker metadata in `~/.foreman/foreman.sqlite3`, logs in `~/.foreman/logs`, and worktrees in `~/.foreman/worktrees`. Multiple browser tabs and MCP calls can attach to the same daemon. Workers keep running if the page closes or the daemon idles out.

The browser cockpit has an intervention bar. `Add note` appends a timestamped operator note to the worker log without changing the worker. `Stop worker` sends a hard interrupt to the worker process group, marks the worker `interrupted`, writes an interrupt result file, and preserves the log and worktree for later collection. This is reliable cancellation, not soft in-process steering; live mid-run conversation with Claude requires a future managed session runner rather than `subprocess.run`.

The daemon shuts itself down after 10 minutes without HTTP activity by default:

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py ensure-daemon --idle-timeout-sec 600
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py ensure-daemon --idle-timeout-sec 0
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py web <worker_id> --idle-timeout-sec 1800 --open
```

Set `--idle-timeout-sec 0` for an always-on daemon. Foreman also records the `scripts/foreman.py` mtime in `~/.foreman/daemon.json`; `ensure-daemon` restarts a stale daemon when the CLI code changes, so the browser monitor does not keep serving old JavaScript after a Foreman upgrade.

`foreman_monitor_hint` also goes through the CLI, so an MCP client can ask for a monitor hint and get a live URL without restarting the plugin. Restart Codex only when the MCP interface changes; daemon and monitor behavior can change in `scripts/foreman.py`.

Claude workers use Claude Code's streaming JSON output by default:

```bash
claude -p "<prompt>" --output-format stream-json --include-partial-messages --include-hook-events
```

On current Claude Code builds, Foreman also passes `--verbose` because Claude requires it with `--print` and `--output-format stream-json`. Foreman writes that stdout directly to the worker log as it arrives. The browser monitor polls byte offsets from that log, updates the URL when you select a different worker, and shows the last successful poll time so a stale stream is visible.

Worker logs include the data Foreman sent in. At worker start Foreman writes an `input_prompt_begin` / `input_prompt_end` block before engine output, so the monitor shows both the prompt/spec and the worker's response stream. Set `FOREMAN_LOG_INPUT=0` only for an unusually sensitive local run.

For multi-worker runs, pass the same `--run-id` to related workers and provide a `--contract` describing the shared interface, file ownership, open questions, and "do not assume" warnings. Foreman stores the shared contract at `~/.foreman/runs/<run_id>/contract.md`, copies it into each worktree at `.foreman/contract.md`, and creates a private `.foreman/worker-notes.md` for that worker. These `.foreman` files are coordination substrate and are stripped before merge/PR commits.

Set `FOREMAN_ENGINE_<ENGINE>_CMD` to override an engine command. Example:

```bash
FOREMAN_ENGINE_CLAUDE_CMD="claude" python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py delegate --engine claude ...
```

If the override contains a literal `{prompt}` token, Foreman replaces that token
with the prompt. Otherwise it appends `-p <prompt>` for compatibility with the
original engine contract.

Local engines are throttled so they do not clog the machine. `opencode` and
`gemma4` share a local-agent slot pool with one active worker by default, and
Foreman runs their command through `nice -n 10` with conservative thread-related
environment variables. Useful knobs:

```bash
FOREMAN_LOCAL_ENGINE_MAX_RUNNING=1
FOREMAN_LOCAL_ENGINE_NICE=10
FOREMAN_LOCAL_ENGINE_THREADS=2
FOREMAN_GEMMA4_MODEL=ollama/gemma3n:e4b
```

`FOREMAN_GEMMA4_MODEL` accepts any opencode model string. The default uses the
small local Ollama Gemma model installed on this machine; set it to a different
Gemma 4 / Gemma-family model when that is the intended local provider.

## Fail-Fast Behavior

`delegate` fails before creating a worktree when:

- the repo path is not a git repo,
- the base ref does not resolve to a commit,
- the selected engine executable is missing,
- the parent repo has uncommitted changes, unless `--allow-dirty` is passed.

Workers also have a timeout. The default is 900 seconds; override with `--timeout-sec`.

## Worker And Merger Agents

Foreman treats implementation workers and merger agents as different roles.

Worker agents own scoped branches/worktrees. They should keep file ownership tight, run the requested verification, and report changed files and questions.

Merger agents integrate completed worker branches back into the parent codebase. They own conflict resolution, cross-worker API seams, final verification, and the decision to merge, open a PR, discard, or send work back for repair. The two-worker Starforge dogfood test proved why this role matters: both workers passed in isolation, but the merger step caught the real integration issue between dict-shaped UI assumptions and dataclass-shaped core state.
