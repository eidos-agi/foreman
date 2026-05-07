# Foreman

Foreman is an Eidos AGI Codex plugin/runtime for delegating implementation work to AI engineer workers while Codex or another caller remains architect and QA.

It is part of the Eidos AGI plugin family alongside Rhea:

- `rhea@eidos-agi`: sovereign model routing, debate, pairing, and image tools.
- `foreman@eidos-agi`: multi-agent coding delegation and git worktree execution.

The plugin exposes MCP tools:

- `foreman_delegate`
- `foreman_list`
- `foreman_tail`
- `foreman_collect`
- `foreman_finalize`

The runtime stores state in `~/.foreman/foreman.sqlite3`, logs in `~/.foreman/logs`, and worktrees in `~/.foreman/worktrees`.

Supported engines:

- `claude`: Claude Code, default implementation worker.
- `codex`: Codex CLI, stronger reasoning fallback or QA worker.
- `gemini`: Gemini CLI, broad-context alternate worker/reviewer.
- `aider`: Aider, narrow git-oriented patch worker.
- `smoke`: deterministic local fake worker for plumbing tests.

Default engine commands:

- `claude -p <prompt>`
- `codex exec --sandbox workspace-write <prompt>`
- `gemini --skip-trust --approval-mode yolo -p <prompt>`
- `aider --yes-always --message <prompt>`

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
[plugins."foreman@eidos-agi"]
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

The tools list should include `foreman_delegate`, `foreman_list`, `foreman_tail`, `foreman_monitor_hint`, `foreman_collect`, and `foreman_finalize`.

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
  --timeout-sec 900 \
  --test-command "python3 -m pytest" \
  "Add a small feature and verify it."

python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py list
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py watch <worker_id>
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py collect <worker_id>
```

## Real-Time Monitoring

Use `watch` from a terminal:

```bash
python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py watch <worker_id>
```

`watch` streams the worker log and status until the worker exits. Pressing `Ctrl-C` stops watching only; it does not cancel the worker.

For MCP/Codex snapshots, use `foreman_tail`. MCP request/response calls are not a true streaming terminal, so the MCP server also exposes `foreman_monitor_hint` with the exact terminal command.

Set `FOREMAN_ENGINE_<ENGINE>_CMD` to override an engine command. Example:

```bash
FOREMAN_ENGINE_CLAUDE_CMD="claude" python3 /Users/dshanklinbv/repos-eidos-agi/foreman/scripts/foreman.py delegate --engine claude ...
```

## Fail-Fast Behavior

`delegate` fails before creating a worktree when:

- the repo path is not a git repo,
- the base ref does not resolve to a commit,
- the selected engine executable is missing,
- the parent repo has uncommitted changes, unless `--allow-dirty` is passed.

Workers also have a timeout. The default is 900 seconds; override with `--timeout-sec`.
