# Contributing

Foreman is a Python CLI and MCP runtime for delegating coding work to AI
workers in isolated git worktrees.

## Setup

```bash
git clone git@github.com:eidos-agi/foreman.git
cd foreman
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

## Verify

```bash
pytest
python scripts/smoke_test.py
python -m build
twine check dist/*
python -m venv /tmp/foreman-wheel
/tmp/foreman-wheel/bin/pip install dist/*.whl
/tmp/foreman-wheel/bin/foreman --help
printf '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n' | /tmp/foreman-wheel/bin/foreman-mcp
```

## Development Notes

- Keep the root `scripts/` files as compatibility wrappers.
- Put new CLI runtime behavior under `packages/foreman-cli/`.
- Put MCP surface behavior under `packages/foreman-mcp/`.
- Do not require real agent API credentials in tests; use `smoke` for local
  deterministic runtime checks.
- Before release, run a tracked-file secret scan and dependency audit from a
  clean local environment.
